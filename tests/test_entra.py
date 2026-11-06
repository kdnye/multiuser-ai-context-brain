"""Entra token gate: every deny path, with locally signed test tokens (no network)."""

from __future__ import annotations

import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_server.entra import (
    DISCOVERY,
    EntraGate,
    EntraValidator,
    discovery_document,
    scope_names,
)

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


def validator(*scopes):
    return EntraValidator(TENANT, [AUD], ["example.com"], key_for=lambda t: KEY.public_key(), required_scopes=list(scopes))


@pytest.mark.parametrize("scp", ["brain.access", "openid brain.access", "brain.access email profile", "  brain.access  "])
def test_a_token_with_the_required_scope_is_admitted(scp):
    assert validator("brain.access").identity(token(scp=scp)) is not None


@pytest.mark.parametrize("scp", [
    None,                              # app-only token or no scope claim at all
    "",                                # empty
    "other.access",                    # consented for another connector on the same registration
    "openid email profile",            # sign-in scopes only
    "xbrain.access",                   # a longer name that merely ends the same way
    "brain.access.extra",              # a longer name that merely starts the same way
    "BRAIN.ACCESS",                    # scope names are case-sensitive
    ["brain.access"],                  # scp is a space-separated string, never a list
    {"brain.access": True},
    42,
])
def test_a_token_without_the_required_scope_is_refused(scp):
    assert validator("brain.access").identity(token(scp=scp)) is None


def test_an_app_only_token_with_roles_is_refused_when_a_scope_is_required():
    # Client-credentials tokens carry `roles` and no `scp`; this server only serves signed-in people.
    assert validator("brain.access").identity(token(roles=["finance"])) is None


def test_two_connectors_on_one_registration_cannot_use_each_others_tokens():
    brain, other = validator("brain.access"), validator("other.access")
    brain_token, other_token = token(scp="brain.access"), token(scp="other.access")
    assert brain.identity(brain_token) is not None and other.identity(other_token) is not None
    assert brain.identity(other_token) is None      # the other connector's token does not open the brain
    assert other.identity(brain_token) is None      # and a brain token does not open the other connector
    both = token(scp="brain.access other.access")   # only if the client was consented for both
    assert brain.identity(both) is not None and other.identity(both) is not None


def test_any_one_of_several_required_scopes_is_enough():
    two = validator("brain.access", "other.access")
    assert two.identity(token(scp="other.access")) is not None and two.identity(token(scp="unrelated")) is None


@pytest.mark.parametrize("bad", [[], [""], ["  "], ["two words"]])
def test_an_empty_or_malformed_scope_list_is_a_configuration_error_not_no_restriction(bad):
    with pytest.raises(ValueError):
        validator(*bad)


def test_without_required_scopes_the_scope_claim_is_not_checked():
    # The unit-test default only; the hosted server always sets required_scopes (see test_mcp_http).
    assert EntraValidator(TENANT, [AUD], ["example.com"], key_for=lambda t: KEY.public_key()).identity(token()) is not None


def test_gate_gives_the_same_denial_for_a_wrong_scope_as_for_any_other_failure():
    gate = EntraGate(None, validator("brain.access"), "https://brain.example/.well-known/oauth-protected-resource")
    assert _call(gate, "/mcp", f"Bearer {token(scp='brain.access')}")[0] == 200
    wrong = _call(gate, "/mcp", f"Bearer {token(scp='other.access')}")
    junk = _call(gate, "/mcp", "Bearer junk")
    assert wrong[0] == 401 and wrong[1] == junk[1]    # nothing says which check failed


@pytest.mark.parametrize("advertised,expected", [
    (["https://brain.example.com/mcp/brain.access"], ["brain.access"]),
    (["api://context-brain/brain.access"], ["brain.access"]),
    (["brain.access"], ["brain.access"]),
    (["https://x/mcp/other.access/"], ["other.access"]),
    (["a/brain.access", "a/other.access"], ["brain.access", "other.access"]),
    (["", "  "], []),
])
def test_scope_names_are_the_last_segment_of_each_advertised_scope(advertised, expected):
    assert scope_names(advertised) == expected


def test_discovery_points_at_the_tenant():
    d = discovery_document("https://brain.example", TENANT, ["api://context-brain/brain.access"])
    assert d["authorization_servers"] == [f"https://login.microsoftonline.com/{TENANT}/v2.0"]
    assert d["resource"] == "https://brain.example/mcp"
