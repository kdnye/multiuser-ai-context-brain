"""Entra ID (Microsoft) token gate for the context brain. Pure ASGI; deny by default.

A request to anything except the public discovery document must carry `Authorization: Bearer <token>` where the
token is a Microsoft identity platform v2 access token that:
  1. is signed by a key from the tenant's published JWKS (RS256 only);
  2. has `iss` == https://login.microsoftonline.com/<tenant>/v2.0 and `tid` == the configured tenant id;
  3. has an `aud` in the configured audiences (this app's client id, or its `api://...` App ID URI);
  4. is inside its `nbf`/`exp` window (small leeway);
  5. names a user (`oid`) whose email (`preferred_username`, `email` or `upn`) is on an allowed domain;
  6. when `required_scopes` is set (the hosted server always sets it), carries one of those delegated scopes in
     `scp`. Several connectors can share one app registration, and `aud` is then the same for all of them, so the
     audience alone does not say which connector a token was consented for. A token with no `scp` (an app-only
     token) is refused.
Silo roles come from the token's `roles` claim (Entra app roles), so access is granted in Entra.

Every failure returns the same 401 body with a `WWW-Authenticate` pointing at the discovery document, so a caller
cannot learn which check failed. Tokens and emails are never logged.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable

import anyio
import jwt

from mcp_server.access import Identity

DISCOVERY = "/.well-known/oauth-protected-resource"
PUBLIC_PATHS = {DISCOVERY}
CACHE_MAX = 1024        # verified-token cache entries; keys derive from caller-supplied tokens, so it must be bounded
_DENY = json.dumps({"error": "unauthorized"}).encode()


def scope_names(scopes: list[str]) -> list[str]:
    """The scope names a token's `scp` claim carries, from the scope values this server advertises.

    `scp` holds the bare name (`brain.access`), while the advertised value is a full URI
    (`https://host/mcp/brain.access` or `api://client/brain.access`), so the name is the part after the last `/`."""
    return [s.strip().rstrip("/").rsplit("/", 1)[-1] for s in scopes if s and s.strip()]


def normalise_scopes(scopes: list[str]) -> frozenset[str]:
    """The bare scope names to require, or ValueError. An empty list is a configuration mistake, not "no restriction"."""
    names = {s.strip() for s in scopes if isinstance(s, str) and s.strip()}
    if not names or any(any(c.isspace() for c in n) for n in names):
        raise ValueError("required_scopes must be a non-empty list of bare scope names")
    return frozenset(names)


class EntraValidator:
    def __init__(self, tenant_id: str, audiences: list[str], allowed_domains: list[str],
                 key_for: Callable[[str], object] | None = None, leeway: int = 60, required_role: str | None = None,
                 *, required_scopes: list[str] | None = None):
        if not tenant_id or not audiences or not allowed_domains:
            raise ValueError("tenant_id, audiences and allowed_domains are all required")
        # None means "do not check scp" and exists for unit tests that build validators directly. The hosted server
        # sets this attribute after construction (like required_role below), so the constructor call tests
        # monkeypatch stays unchanged; an empty list is refused rather than treated as None.
        self.required_scopes: frozenset[str] | None = normalise_scopes(required_scopes) if required_scopes is not None else None
        self.tenant_id = tenant_id
        # Optional defence in depth (BRAIN_REQUIRED_ROLE): demand this app role in the token, so a tenant account that
        # Entra's "Assignment required" setting would have excluded cannot sign in if that setting is ever turned off.
        self.required_role = (required_role or "").strip() or None
        self.issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self.audiences = list(audiences)
        self.domains = {d.strip().lower().lstrip("@") for d in allowed_domains if d.strip()}
        self.leeway = leeway
        if key_for is None:
            client = jwt.PyJWKClient(f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys", cache_keys=True)
            key_for = lambda token: client.get_signing_key_from_jwt(token).key
        self._key_for = key_for
        self._cache: dict[str, tuple[float, Identity]] = {}
        self._lock = threading.Lock()

    def _allowed_email(self, email: str) -> bool:
        """ASCII `local@domain` with exactly one "@", no whitespace, and a domain on the allow-list."""
        local, sep, domain = email.partition("@")
        return bool(sep and local and email.isascii() and "@" not in domain and not any(c.isspace() for c in email)
                    and domain in self.domains)

    def _allowed_roles(self, roles: frozenset) -> bool:
        return self.required_role is None or self.required_role in roles

    def _remember(self, digest: str, exp: float, ident: Identity) -> None:
        """Cache a verified identity until the token expires. Expired entries are dropped on every insert and the
        size is capped (soonest-to-expire entries go first), so a stream of distinct tokens cannot grow it."""
        now = time.time()
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            while len(self._cache) >= CACHE_MAX:
                self._cache.pop(min(self._cache, key=lambda k: self._cache[k][0]))
            self._cache[digest] = (exp, ident)

    async def identity_async(self, token: str) -> Identity | None:
        """identity() without blocking the event loop: the JWKS fetch inside it is network I/O."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        hit = self._cache.get(digest)
        if hit and hit[0] > time.time():
            return hit[1]
        return await anyio.to_thread.run_sync(self.identity, token)

    def identity(self, token: str) -> Identity | None:
        """The verified caller, or None. Never raises."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        hit = self._cache.get(digest)
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            claims = jwt.decode(token, self._key_for(token), algorithms=["RS256"], audience=self.audiences,
                                issuer=self.issuer, leeway=self.leeway,
                                options={"require": ["exp", "iss", "aud", "tid", "oid"]})
        except Exception:  # noqa: BLE001 - any failure to verify is a denial, by design
            return None
        if claims.get("tid") != self.tenant_id:
            return None
        if self.required_scopes is not None:
            scp = claims.get("scp")
            if not isinstance(scp, str) or not self.required_scopes & set(scp.split()):
                return None
        email = str(claims.get("preferred_username") or claims.get("email") or claims.get("upn") or "").strip().lower()
        if not self._allowed_email(email):
            return None
        roles = claims.get("roles") or []
        try:
            ident = Identity(user_id=claims["oid"], email=email,
                             roles=frozenset(r for r in roles if isinstance(r, str)))   # refuses an oid that is not a GUID
            if not self._allowed_roles(ident.roles):
                return None
            exp = float(claims["exp"])
        except (ValueError, TypeError):
            return None
        self._remember(digest, exp, ident)
        return ident


def _bearer(scope) -> str:
    for k, v in scope.get("headers") or []:
        if k.lower() == b"authorization":
            val = v.decode("latin-1")
            if val[:7].lower() == "bearer ":
                return val[7:].strip()
    return ""


class EntraGate:
    """ASGI middleware: puts the verified `Identity` in scope["state"]["identity"] or answers 401."""

    def __init__(self, app, validator: EntraValidator, resource_metadata_url: str):
        self.app, self.validator, self.metadata_url = app, validator, resource_metadata_url

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return None
        if scope.get("path") in PUBLIC_PATHS:
            return await self.app(scope, receive, send)
        token = _bearer(scope)
        ident = await self.validator.identity_async(token) if token else None
        if ident is None:
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", f'Bearer resource_metadata="{self.metadata_url}"'.encode())]})
            await send({"type": "http.response.body", "body": _DENY})
            return None
        scope.setdefault("state", {})["identity"] = ident
        return await self.app(scope, receive, send)


def discovery_document(public_url: str, tenant_id: str, scopes: list[str]) -> dict:
    """RFC 9728 protected-resource metadata: tells the client to sign in with the configured Entra tenant."""
    return {"resource": public_url.rstrip("/") + "/mcp",
            "authorization_servers": [f"https://login.microsoftonline.com/{tenant_id}/v2.0"],
            "scopes_supported": scopes}
