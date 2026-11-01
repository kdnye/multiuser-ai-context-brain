"""Compact week mode for wiki_timeline: sessions in full, everything else counted, nothing cut."""

from __future__ import annotations

from mcp_server.store import CachedStore
from mcp_server.wiki_store import Wiki
from tests.test_wiki_relations import Mem


def page(title, kind, body="A page body that is long enough.", **fm):
    lines = [f'title: "{title}"', f'type: "{kind}"', 'scope: "work"', 'source_ids: ["x"]', *[f'{k}: "{v}"' for k, v in fm.items()]]
    return "---\n" + "\n".join(lines) + f"\n---\n\n# {title}\n\n{body}\n"


def build():
    w = Wiki(CachedStore(Mem()), today=lambda: "2026-10-29")
    w.write("sessions/2026-09-11-a", page("Daily chat on 2026-09-11 concerning A", "session", "Worked on A.", date="2026-09-11"), "s")
    w.write("sessions/2026-09-11-b", page("Daily chat on 2026-09-11 concerning B", "session", "Worked on B.", date="2026-09-11"), "s")
    for i in range(40):                                   # more than per_day: the normal mode would cut these
        w.write(f"notes/t{i}", page(f"Item {i}", "note", created="2026-09-11"), "s")
    w.write("notes/n", page("Note", "note", created="2026-09-11"), "s")
    w.write("notes/old", page("Old", "note", "## Update (2026-09-12)\n\nChanged.", created="2026-08-01"), "s")
    return w


def test_compact_lists_sessions_in_full_and_counts_the_rest_without_cutting():
    w = build()
    t = w.timeline("2026-09-07", "2026-09-13", compact=True)
    assert t["compact"] is True
    d11 = {d["date"]: d for d in t["days"]}["2026-09-11"]
    assert [s["path"] for s in d11["sessions"]] == ["sessions/2026-09-11-a", "sessions/2026-09-11-b"]
    assert d11["sessions"][0]["summary"].startswith("Worked on A")
    assert d11["counts"] == {"created": {"note": 41}} and d11["count"] == 43
    d12 = {d["date"]: d for d in t["days"]}["2026-09-12"]
    assert d12["sessions"] == [] and d12["counts"] == {"changed": {"note": 1}}
    assert "items were left out (limits" not in t["note"]                                 # nothing cut


def test_compact_is_off_by_default_and_empty_range_still_explains():
    w = build()
    assert "compact" not in w.timeline("2026-09-11", "2026-09-11")
    t = w.timeline("2025-01-01", "2025-01-07", compact=True)
    assert t["days"] == [] and t["note"].startswith("Nothing dated between")


def test_compact_through_the_router_stays_in_the_callers_silo():
    from mcp_server.access import Identity
    from mcp_server.router import Brain
    from mcp_server.store import CachedStore as CS
    stores = {}
    brain = Brain(lambda silo: stores.setdefault(silo, CS(Mem())), audit=lambda e: None, today=lambda: "2026-10-29")
    a = Identity(user_id="00000000-0000-4000-8000-00000000000a", email="a@example.com")
    b = Identity(user_id="00000000-0000-4000-8000-00000000000b", email="b@example.com")
    brain.write(a, "notes/a", page("A note", "note", created="2026-09-11"), "s")
    brain.write(b, "notes/b", page("B note", "note", created="2026-09-11"), "s")
    t = brain.timeline(a, "2026-09-07", "2026-09-13", compact=True)
    assert t["silo"] == a.home and t["days"][0]["counts"] == {"created": {"note": 1}}
