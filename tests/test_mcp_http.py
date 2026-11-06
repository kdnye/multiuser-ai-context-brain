"""The hosted app end to end over HTTP: Entra gate, a real MCP session, per-user silos, startup refusals."""

from __future__ import annotations

import json
import time
import uuid

import jwt
import pytest

pytest.importorskip("mcp", reason="install requirements-mcp.txt to run the hosted-app tests")

from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from mcp_server import entra, server
from tests.test_store import FakeBackend

URL = "https://brain.example.com"
TENANT = "11111111-2222-3333-4444-555555555555"
AUD = "api://context-brain"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ENV = {"MCP_TRANSPORT": "http", "MCP_PUBLIC_URL": URL, "ENTRA_TENANT_ID": TENANT, "ENTRA_AUDIENCES": AUD,
       "ALLOWED_EMAIL_DOMAINS": "example.com", "BRAIN_STORE": "gcs", "BRAIN_BUCKET": "b"}
NOTE = '---\ntitle: "{t}"\ntype: "note"\nscope: "work"\nsource_ids: ["x"]\n---\n\n# {t}\n\n{body}\n'


def oid(email):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, email))


ANA_HOME = "users/" + oid("ana.ops@example.com")


def token(email, roles=(), scp="brain.access"):
    now = int(time.time())
    claims = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": AUD, "tid": TENANT, "oid": oid(email),
              "preferred_username": email, "roles": list(roles), "scp": scp, "nbf": now - 5, "exp": now + 600}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


@pytest.fixture(scope="module")
def client():
    buckets: dict[str, FakeBackend] = {}
    mp = pytest.MonkeyPatch()
    for k, v in ENV.items():
        mp.setenv(k, v)
    mp.delenv("BRAIN_DEV_EMAIL", raising=False)
    mp.setattr(server, "GcsBackend", lambda bucket, prefix: buckets.setdefault(prefix, FakeBackend()))
    mp.setattr(server, "_brain", None)
    real = entra.EntraValidator
    mp.setattr(entra, "EntraValidator", lambda t, a, d: real(t, a, d, key_for=lambda tok: KEY.public_key()))
    with TestClient(server._http_app(), base_url=URL) as c:
        c.buckets = buckets
        yield c
    mp.undo()


def rpc(r):
    assert r.status_code in (200, 202), (r.status_code, r.text[:200])
    body = r.text.strip()
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(body) if body else {}


def session(client, email, roles=()):
    h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "Authorization": f"Bearer {token(email, roles)}"}
    r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
    h = {**h, "mcp-session-id": r.headers["mcp-session-id"]}
    client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return h


def call(client, h, name, args):
    msg = rpc(client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}}))
    return json.loads(msg["result"]["content"][0]["text"])


@pytest.mark.parametrize("scp", [None, "other.access", "openid email profile"])
def test_a_token_for_another_connector_scope_gets_401_from_the_hosted_app(client, scp):
    # The hosted app requires the scope it advertises (default brain.access): a second connector on the same registration
    # has the same audience, so a token consented for it must not open the brain.
    h = {"Authorization": f"Bearer {token('ana.ops@example.com', scp=scp)}"}
    assert client.post("/mcp", headers=h, json={}).status_code == 401
    ok = {"Authorization": f"Bearer {token('ana.ops@example.com')}"}
    assert client.post("/mcp", headers=ok, json={}).status_code != 401


def test_unauthenticated_and_foreign_tokens_get_401_and_discovery_is_public(client):
    assert client.post("/mcp", json={}).status_code == 401
    bad = {"Authorization": f"Bearer {token('someone@personal-mail.test')}"}
    assert client.post("/mcp", headers=bad, json={}).status_code == 401
    d = client.get("/.well-known/oauth-protected-resource").json()
    assert d["authorization_servers"] == [f"https://login.microsoftonline.com/{TENANT}/v2.0"]


def test_tools_act_as_the_signed_in_user_and_silos_do_not_leak(client):
    ana = session(client, "ana.ops@example.com")
    ben = session(client, "ben.ar@example.com", roles=["finance"])
    w = call(client, ana, "wiki_write", {"path": "notes/my-accounts", "content": NOTE.format(t="My accounts", body="Northwind and Contoso accounts."), "summary": "x"})
    assert w["silo"] == ANA_HOME and w["action"] == "created"
    assert "notes/my-accounts.md" in client.buckets[f"silos/{ANA_HOME}"].pages
    assert call(client, ben, "wiki_search", {"query": "accounts"})["results"] == []
    assert "no access" in call(client, ben, "wiki_read", {"path": "notes/my-accounts", "silo": ANA_HOME})["error"]
    assert "no access" in call(client, ana, "wiki_write", {"path": "notes/x", "content": NOTE.format(t="x", body="nope"),
                                                          "summary": "x", "silo": "finance"})["error"]
    assert call(client, ben, "wiki_write", {"path": "notes/ar", "content": NOTE.format(t="AR aging", body="Weekly."),
                                            "summary": "x", "silo": "finance"})["silo"] == "finance"
    assert all(r["silo"] != "finance" for r in call(client, ana, "wiki_search", {"query": "aging"})["results"])


@pytest.mark.parametrize("area,other", [("ops", "hr"), ("hr", "ops")])
def test_new_area_roles_are_enforced_from_signed_tokens(client, area, other):
    member = session(client, "area.member@example.com", roles=["brain-user", area])
    outsider = session(client, "area.outsider@example.com", roles=["brain-user", other])
    path = f"notes/{area}-checklist"
    note = NOTE.format(t=f"{area} checklist", body=f"Restricted {area} checklist procedure.")
    assert call(client, member, "wiki_write", {"path": path, "content": note, "summary": "test", "silo": area})["silo"] == area
    assert f"Restricted {area}" in call(client, member, "wiki_read", {"path": path, "silo": area})["content"]
    for name, args in (
        ("wiki_read", {"path": path, "silo": area}),
        ("wiki_write", {"path": path, "content": note, "summary": "denied", "silo": area}),
    ):
        assert "no access" in call(client, outsider, name, args)["error"]
    assert all(hit["silo"] != area for hit in call(client, outsider, "wiki_search", {"query": f"{area} checklist"})["results"])


def test_hosted_app_refuses_a_dev_identity_or_missing_config(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BRAIN_DEV_EMAIL", "dev@example.com")
    with pytest.raises(ValueError, match="BRAIN_DEV_EMAIL"):
        server._http_app()
    monkeypatch.delenv("BRAIN_DEV_EMAIL")
    monkeypatch.delenv("ENTRA_TENANT_ID")
    with pytest.raises(ValueError, match="ENTRA_TENANT_ID"):
        server._http_app()


def test_stdio_dev_identity_is_ignored_when_transport_is_http(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("BRAIN_DEV_EMAIL", "dev@example.com")
    with pytest.raises(PermissionError):
        server._identity(None)
