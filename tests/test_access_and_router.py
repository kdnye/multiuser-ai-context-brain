"""Silo isolation: who can read and write what, and that nothing leaks across silos."""

from __future__ import annotations

import pytest

from mcp_server.access import (
    Identity,
    can_read,
    can_write,
    readable,
    resolve,
)
from mcp_server.router import Brain
from mcp_server.store import CachedStore

NOTE = '---\ntitle: "{t}"\ntype: "note"\nscope: "work"\nsource_ids: ["x"]\n---\n\n# {t}\n\n{body}\n'


class Mem:
    def __init__(self):
        self.d = {}

    def list_names(self):
        return list(self.d)

    def get(self, n):
        return self.d.get(n)

    def put(self, n, t):
        self.d[n] = t


ANA = Identity("a0000000-0000-4000-8000-000000000001", "ana.ops@example.com")
BEN = Identity("a0000000-0000-4000-8000-000000000002", "ben.ar@example.com", frozenset({"finance"}))
EDI = Identity("a0000000-0000-4000-8000-000000000003", "ed.editor@example.com", frozenset({"shared-editor"}))


@pytest.fixture
def brain():
    buckets: dict[str, Mem] = {}
    events: list[dict] = []
    b = Brain(lambda silo: CachedStore(buckets.setdefault(silo, Mem())), today=lambda: "2026-10-28", audit=events.append)
    b.events, b.buckets = events, buckets
    return b


def test_home_is_the_object_id():
    assert ANA.home == "users/a0000000-0000-4000-8000-000000000001"
    assert Identity("A0000000-0000-4000-8000-00000000000A", "x@example.com").home == "users/a0000000-0000-4000-8000-00000000000a"


def test_policy_matrix():
    assert readable(ANA) == ["users/a0000000-0000-4000-8000-000000000001", "shared"]
    assert readable(BEN) == ["users/a0000000-0000-4000-8000-000000000002", "shared", "finance"]
    assert can_write(ANA, "users/a0000000-0000-4000-8000-000000000001") and not can_write(ANA, "shared") and not can_write(ANA, "users/a0000000-0000-4000-8000-000000000002")
    assert can_write(EDI, "shared") and not can_read(ANA, "finance") and can_write(BEN, "finance")
    assert resolve(ANA, "me") == resolve(ANA, None) == "users/a0000000-0000-4000-8000-000000000001"
    with pytest.raises(PermissionError):
        resolve(ANA, "../etc")


def test_users_cannot_read_or_write_each_others_silos(brain):
    brain.write(ANA, "notes/my-accounts", NOTE.format(t="My accounts", body="Northwind and Contoso accounts handled by me."), "x")
    with pytest.raises(PermissionError):
        brain.read(BEN, "notes/my-accounts", silo="users/a0000000-0000-4000-8000-000000000001")
    with pytest.raises(PermissionError):
        brain.write(BEN, "notes/x", NOTE.format(t="x", body="intrusion attempt"), "x", silo="users/a0000000-0000-4000-8000-000000000001")
    with pytest.raises(PermissionError):
        brain.index(BEN, silo="users/a0000000-0000-4000-8000-000000000001")
    assert brain.search(BEN, "accounts")["results"] == []              # other silo's pages never appear in results
    assert brain.context(BEN, "Northwind accounts")["results"] == []
    assert brain.search(ANA, "accounts")["results"][0]["silo"] == "users/a0000000-0000-4000-8000-000000000001"


def test_shared_is_readable_by_all_and_writable_only_by_editors(brain):
    with pytest.raises(PermissionError):
        brain.write(ANA, "notes/erp-tips", NOTE.format(t="ERP tips", body="Saved search basics."), "x", silo="shared")
    brain.write(EDI, "notes/erp-tips", NOTE.format(t="ERP tips", body="Saved search basics."), "x", silo="shared")
    for who in (ANA, BEN):
        assert brain.read(who, "notes/erp-tips", silo="shared")["content"].startswith("---")
        assert any(h["silo"] == "shared" for h in brain.search(who, "erp")["results"])


def test_finance_silo_is_role_gated(brain):
    brain.write(BEN, "notes/ar-aging", NOTE.format(t="AR aging process", body="Weekly aging review."), "x", silo="finance")
    with pytest.raises(PermissionError):
        brain.read(ANA, "notes/ar-aging", silo="finance")
    assert all(h["silo"] != "finance" for h in brain.search(ANA, "aging")["results"])
    assert brain.search(BEN, "aging")["results"][0]["silo"] == "finance"


@pytest.mark.parametrize("role", ["finance", "hr", "ops"])
def test_area_role_grants_only_its_own_area(role):
    who = Identity(ANA.user_id, ANA.email, frozenset({"brain-user", role}))
    assert readable(who) == [who.home, "shared", role]
    for area in ("finance", "hr", "ops"):
        assert can_read(who, area) == (area == role)
        assert can_write(who, area) == (area == role)
    assert not can_write(who, "shared")


@pytest.mark.parametrize("area,other", [("ops", "hr"), ("hr", "ops")])
def test_new_areas_isolate_writes_reads_and_recall(brain, area, other):
    member = Identity(ANA.user_id, ANA.email, frozenset({"brain-user", area}))
    outsiders = [ANA, BEN, EDI, Identity(BEN.user_id, BEN.email, frozenset({other}))]
    note = NOTE.format(t="Restricted launch checklist", body="Restricted launch checklist procedure.")
    brain.write(member, "notes/launch-checklist", note, "test", silo=area)
    assert "Restricted launch checklist" in brain.read(member, "notes/launch-checklist", silo=area)["content"]
    assert brain.search(member, "launch checklist")["results"][0]["silo"] == area
    assert brain.context(member, "launch checklist")["results"][0]["silo"] == area
    for outsider in outsiders:
        for action in (
            lambda who=outsider: brain.read(who, "notes/launch-checklist", silo=area),
            lambda who=outsider: brain.write(who, "notes/launch-checklist", note, "denied", silo=area),
            lambda who=outsider: brain.index(who, silo=area),
        ):
            with pytest.raises(PermissionError, match="no access"):
                action()
            assert brain.events[-1]["allowed"] is False
        assert brain.search(outsider, "launch checklist")["results"] == []
        assert brain.context(outsider, "launch checklist")["results"] == []
    for invalid in (area + "\n", area + "/extra", area.upper(), "../" + area):
        with pytest.raises(PermissionError):
            resolve(member, invalid)


def test_silos_are_separate_stores_and_every_check_is_audited(brain):
    brain.write(ANA, "notes/a", NOTE.format(t="A", body="Body text for A."), "x")
    brain.write(BEN, "notes/b", NOTE.format(t="B", body="Body text for B."), "x")
    assert "notes/a.md" in brain.buckets["users/a0000000-0000-4000-8000-000000000001"].d and "notes/a.md" not in brain.buckets["users/a0000000-0000-4000-8000-000000000002"].d
    assert "users/a0000000-0000-4000-8000-000000000002" not in brain.buckets["users/a0000000-0000-4000-8000-000000000001"].d.get("index.md", "")
    with pytest.raises(PermissionError):
        brain.read(ANA, "notes/b", silo="users/a0000000-0000-4000-8000-000000000002")
    refused = [e for e in brain.events if not e["allowed"]]
    assert refused[-1] == {"user_id": "a0000000-0000-4000-8000-000000000001", "action": "read", "silo": "users/a0000000-0000-4000-8000-000000000002", "path": "notes/b", "allowed": False}


def test_ingest_always_lands_in_the_callers_own_silo(brain):
    r = brain.ingest_session(ANA, {"session": {"title": "Ops day", "date": "2026-10-28", "summary": "Worked pickups."}})
    assert r["silo"] == "users/a0000000-0000-4000-8000-000000000001" and r["session"].startswith("sessions/")


@pytest.mark.parametrize("env", [
    {"BRAIN_ROLE_SILOS": "brain-user"},                       # every user holds it
    {"BRAIN_ROLE_SILOS": "shared-editor"},                    # the editor role must not double as a silo
    {"BRAIN_ROLE_SILOS": "users"}, {"BRAIN_ROLE_SILOS": "Finance"}, {"BRAIN_ROLE_SILOS": "fin/x"},
    {"BRAIN_ROLE_SILOS": "finance", "BRAIN_SHARED_EDITOR_ROLE": "finance"},   # finance members would edit shared
    {"BRAIN_SHARED_EDITOR_ROLE": " "}, {"BRAIN_SHARED_EDITOR_ROLE": "brain-user"},
])
def test_unsafe_silo_configuration_fails_at_startup(monkeypatch, env):
    from mcp_server.access import validate_config
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValueError):
        validate_config()


def test_custom_role_silos(monkeypatch):
    from mcp_server.access import can_write, readable
    monkeypatch.setenv("BRAIN_ROLE_SILOS", "legal,sales")
    monkeypatch.setenv("BRAIN_SHARED_EDITOR_ROLE", "editors")
    who = Identity("a0000000-0000-4000-8000-0000000000aa", "x@example.com", frozenset({"legal", "editors", "finance"}))
    assert readable(who)[1:] == ["shared", "legal"]                      # finance is no longer configured
    assert can_write(who, "shared") and can_write(who, "legal") and not can_write(who, "sales")
    with pytest.raises(PermissionError):
        resolve(who, "finance")
