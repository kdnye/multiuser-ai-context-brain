"""Date-aware tools through the silo router: they only ever see silos the caller may read."""

from __future__ import annotations

import pytest

from mcp_server.access import Identity
from mcp_server.router import Brain
from mcp_server.store import CachedStore
from tests.test_access_and_router import ANA, BEN, Mem

CAL = Identity("a0000000-0000-4000-8000-000000000004", "cal.ops@example.com")


def page(title, kind="note", **fm):
    lines = [f'title: "{title}"', f'type: "{kind}"', 'scope: "work"', 'source_ids: ["x"]', *[f'{k}: "{v}"' for k, v in fm.items()]]
    return "---\n" + "\n".join(lines) + f"\n---\n\n# {title}\n\nEnough words for a real page body, mentioning harbor.\n"


@pytest.fixture
def brain():
    buckets: dict[str, Mem] = {}
    b = Brain(lambda silo: CachedStore(buckets.setdefault(silo, Mem())), today=lambda: "2026-10-28")
    b.write(ANA, "notes/ana-harbor", page("Ana harbor note", date="2026-10-20"), "seed")
    b.write(CAL, "notes/cal-harbor", page("Cal harbor note", date="2026-10-20"), "seed")
    b.write(BEN, "notes/ben-harbor", page("Ben harbor note", date="2026-10-20"), "seed", silo="finance")
    b.write(BEN, "notes/ben-own", page("Ben own note", date="2026-10-20"), "seed")
    return b


def paths(r):
    if "pages" in r:
        return {x["path"] for x in r["pages"]}
    return {i["path"] for d in r["days"] for i in d["items"]}


def test_timeline_defaults_to_own_silo_only(brain):
    t = brain.timeline(ANA, "2026-10-20", "2026-10-20")
    assert t["silo"] == "users/a0000000-0000-4000-8000-000000000001" and paths(t) == {"notes/ana-harbor"}
    assert paths(brain.timeline(CAL, "2026-10-20", "2026-10-20")) == {"notes/cal-harbor"}


def test_list_and_log_default_to_own_silo(brain):
    r = brain.list(ANA, date_from="2026-10-20", date_to="2026-10-20")
    assert r["silo"] == "users/a0000000-0000-4000-8000-000000000001" and paths(r) == {"notes/ana-harbor"}
    log = brain.log(ANA, 50, since="2026-10-28", until="2026-10-28")
    assert log["silo"] == "users/a0000000-0000-4000-8000-000000000001" and log["entries"] and all("ana-harbor" in e for e in log["entries"])


def test_search_by_date_never_returns_another_users_pages(brain):
    r = brain.search(ANA, "2026-10-20")
    assert {(h["silo"], h["path"]) for h in r["results"]} == {("users/a0000000-0000-4000-8000-000000000001", "notes/ana-harbor")}
    miss = brain.search(ANA, "2026-01-01")
    assert miss["results"] == [] and "No entries for 2026-01-01" in miss["note"]
    assert "cal" not in miss["note"].lower() and "finance" not in miss["note"] and "users/a0000000-0000-4000-8000-000000000001" in miss["note"]


@pytest.mark.parametrize("call", [
    lambda b, who, silo: b.timeline(who, "2026-10-20", "2026-10-20", silo=silo),
    lambda b, who, silo: b.list(who, silo=silo),
    lambda b, who, silo: b.log(who, silo=silo),
])
def test_other_users_silo_is_refused_like_any_other_refusal(brain, call):
    with pytest.raises(PermissionError) as other:
        call(brain, ANA, "users/a0000000-0000-4000-8000-000000000004")
    with pytest.raises(PermissionError) as fin:
        call(brain, ANA, "finance")
    assert str(other.value) == "no access to silo 'users/a0000000-0000-4000-8000-000000000004'" and str(fin.value) == "no access to silo 'finance'"
    with pytest.raises(PermissionError, match="no access to silo 'users/a0000000-0000-4000-8000-000000000004'"):
        brain.read(ANA, "notes/cal-harbor", "users/a0000000-0000-4000-8000-000000000004")


def test_finance_pages_only_for_finance_role(brain):
    assert paths(brain.timeline(BEN, "2026-10-20", "2026-10-20", silo="finance")) == {"notes/ben-harbor"}
    assert paths(brain.timeline(BEN, "2026-10-20", "2026-10-20")) == {"notes/ben-own"}
    assert {(h["silo"], h["path"]) for h in brain.search(BEN, "2026-10-20")["results"]} == {
        ("finance", "notes/ben-harbor"), ("users/a0000000-0000-4000-8000-000000000002", "notes/ben-own")}
    assert all(h["silo"] != "finance" for h in brain.search(ANA, "2026-10-20")["results"])
    assert all(h["silo"] != "finance" for h in brain.search(ANA, "harbor")["results"])


def test_date_calls_are_audited(brain):
    events = []
    brain._audit = events.append
    brain.timeline(ANA, "2026-10-20", "2026-10-20")
    brain.list(ANA)
    brain.log(ANA)
    with pytest.raises(PermissionError):
        brain.timeline(ANA, "2026-10-20", "2026-10-20", silo="finance")
    assert [(e["action"], e["allowed"]) for e in events] == [("timeline", True), ("list", True), ("log", True), ("timeline", False)]


def test_malformed_silo_refusal_is_audited_with_the_raw_name(brain):
    events = []
    brain._audit = events.append
    junk = "../../etc/passwd" + "x" * 200
    with pytest.raises(PermissionError) as exc:
        brain.timeline(ANA, "2026-10-20", "2026-10-20", silo=junk)
    assert str(exc.value).startswith("no access to silo '../../etc/passwd")
    assert len(events) == 1 and events[0]["allowed"] is False and events[0]["silo"] == junk[:80] and events[0]["action"] == "timeline"
    events.clear()
    brain.list(ANA, silo="me")                                    # near miss: a valid alias is audited once, allowed
    assert [(e["allowed"], e["silo"]) for e in events] == [(True, "users/a0000000-0000-4000-8000-000000000001")]


def test_log_returns_truncation_flag(brain):
    r = brain.log(ANA, 1, since="2026-10-28", until="2026-10-28")
    assert r["silo"] == "users/a0000000-0000-4000-8000-000000000001" and r["truncated"] is False and len(r["entries"]) == 1
    brain.write(ANA, "notes/ana-two", page("Ana two", date="2026-10-21"), "second")
    assert brain.log(ANA, 1, since="2026-10-28", until="2026-10-28")["truncated"] is True


def test_context_with_a_date_fans_out_per_silo_and_stays_labelled(brain):
    r = brain.context(BEN, "what happened on 2026-10-20")
    assert {(h["silo"], h["path"]) for h in r["results"]} == {("finance", "notes/ben-harbor"), ("users/a0000000-0000-4000-8000-000000000002", "notes/ben-own")}
    assert all(h["silo"] != "finance" for h in brain.context(ANA, "2026-10-20")["results"])
    miss = brain.context(ANA, "2026-01-01")
    assert miss["results"] == [] and miss["note"].startswith("No entries for 2026-01-01") and "finance" not in miss["note"]
    assert brain.context(ANA, "harbor")["results"]                # near miss: no date, loose matching
