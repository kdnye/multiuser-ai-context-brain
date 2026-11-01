"""Entra token gate: every deny path, with locally signed test tokens (no network)."""

from __future__ import annotations

import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_server.entra import DISCOVERY, EntraGate, EntraValidator, discovery_document

TENANT = "11111111-2222-3333-4444-555555555555"
AUD = "api://context-brain"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key=KEY, **over):
    now = int(time.time())
    claims = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": AUD, "tid": TENANT, "oid": "a0000000-0000-4000-8000-000000000001",
              "preferred_username": "Ana.Ops@example.com", "roles": ["finance"], "nbf": now - 5, "exp": now + 600}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256")


@pytest.fixture
def v():
    return EntraValidator(TENANT, [AUD], ["example.com"], key_for=lambda t: KEY.public_key())


def test_valid_token_yields_identity_with_roles(v):
    ident = v.identity(token())
    assert ident.user_id == "a0000000-0000-4000-8000-000000000001" and ident.email == "ana.ops@example.com" and ident.roles == {"finance"}
    assert ident.home == "users/a0000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("bad", [
    {"key": OTHER},                                                   # signed by someone else
    {"aud": "api://something-else"},                                  # issued to another app
    {"iss": "https://login.microsoftonline.com/other/v2.0"},          # another issuer
    {"tid": "99999999-0000-0000-0000-000000000000"},                  # another tenant
    {"exp": int(time.time()) - 3600},                                 # expired
    {"nbf": int(time.time()) + 3600},                                 # not yet valid
    {"oid": None},                                                    # no user object id
    {"preferred_username": "someone@personal-mail.test"},                      # personal account / other domain
    {"preferred_username": "noemail"},                                # no email at all
    {"preferred_username": "ａna@example.com"},               # non-ASCII lookalike
])
def test_every_deny_path_returns_none(v, bad):
    kw = dict(bad)
    key = kw.pop("key", KEY)
    assert v.identity(token(key=key, **kw)) is None


def test_hs256_and_none_algorithms_are_refused(v):
    forged = jwt.encode({"iss": v.issuer, "aud": AUD, "tid": TENANT, "oid": "x", "exp": int(time.time()) + 60,
                         "preferred_username": "a@example.com"}, "secret", algorithm="HS256")
    assert v.identity(forged) is None
    assert v.identity("not.a.token") is None


def test_validator_refuses_incomplete_configuration():
    with pytest.raises(ValueError):
        EntraValidator("", [AUD], ["example.com"], key_for=lambda t: None)


def _call(gate, path, auth=None):
    sent, seen = [], {}

    async def app(scope, receive, send):
        seen["identity"] = scope.get("state", {}).get("identity")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    gate.app = app
    headers = [(b"authorization", auth.encode())] if auth else []

    async def receive():
        return {"type": "http.request"}

    async def send(m):
        sent.append(m)

    asyncio.run(gate({"type": "http", "path": path, "headers": headers}, receive, send))
    return sent[0]["status"], (sent[1].get("body") if len(sent) > 1 else b""), seen.get("identity")


def test_gate_admits_valid_tokens_and_denies_everything_else_identically(v):
    gate = EntraGate(None, v, "https://brain.example/.well-known/oauth-protected-resource")
    status, _, ident = _call(gate, "/mcp", f"Bearer {token()}")
    assert status == 200 and ident.email == "ana.ops@example.com"
    bodies = {_call(gate, "/mcp", a)[1] for a in (None, "Bearer junk", f"Bearer {token(aud='x')}", "Basic abc")}
    assert len(bodies) == 1 and _call(gate, "/mcp")[0] == 401
    assert _call(gate, DISCOVERY)[0] == 200                           # discovery is public


def test_discovery_points_at_the_tenant():
    d = discovery_document("https://brain.example", TENANT, ["api://context-brain/brain.access"])
    assert d["authorization_servers"] == [f"https://login.microsoftonline.com/{TENANT}/v2.0"]
    assert d["resource"] == "https://brain.example/mcp"
