"""Date-aware lookup inside one silo: activity dates, list filters, exact-date search, timeline, log ranges."""

from __future__ import annotations

import pytest

from mcp_server.store import CachedStore
from mcp_server.wiki_store import Wiki, WikiError
from tests.test_wiki_relations import Mem


def page(title, kind, body="Enough words for a real page body here.", **fm):
    lines = [f'title: "{title}"', f'type: "{kind}"', 'scope: "work"', 'source_ids: ["x"]', *[f'{k}: "{v}"' for k, v in fm.items()]]
    return "---\n" + "\n".join(lines) + f"\n---\n\n# {title}\n\n{body}\n"


@pytest.fixture
def wiki():
    today = {"d": "2026-07-14"}
    w = Wiki(CachedStore(Mem()), today=lambda: today["d"])
    w._set_today = lambda d: today.__setitem__("d", d)
    for i in range(3):
        w.write(f"sessions/2026-07-14-s{i}", page(f"Daily chat on 2026-07-14 concerning S{i}", "session", date="2026-07-14"), "seed")
    w.write("sessions/2026-07-20-later", page("Daily chat on 2026-07-20 concerning Later", "session", date="2026-07-20"), "seed")
    w.write("projects/old-open", page("Old open project", "project", status="In progress", created="2026-07-15"), "seed")
    w.write("projects/old-done", page("Old done project", "project", status="Done", created="2026-07-16"), "seed")
    w.write("projects/new-open", page("New open project", "project", status="Not started", created="2026-08-05"), "seed")
    w.write("notes/no-date-given", page("A note written today", "note"), "seed")       # created stamped 2026-07-14
    return w


def test_timeline_returns_the_sessions_of_that_day(wiki):
    t = wiki.timeline("2026-07-14", "2026-07-14")
    assert [d["date"] for d in t["days"]] == ["2026-07-14"]
    items = t["days"][0]["items"]
    assert [i["path"] for i in items if i["what"] == "session"] == ["sessions/2026-07-14-s0", "sessions/2026-07-14-s1", "sessions/2026-07-14-s2"]
    assert "notes/no-date-given" in [i["path"] for i in items]              # created today counts as that day


def test_search_for_a_date_before_coverage_says_so(wiki):
    r = wiki.search_view("2026-07-12")
    assert r["results"] == [] and r["note"].startswith("No entries for 2026-07-12.")
    assert "sessions span 2026-07-14 to 2026-07-20" in r["note"]
    hit = wiki.search_view("2026-07-20")["results"]
    assert hit[0]["path"] == "sessions/2026-07-20-later" and hit[0]["matched"] == "dated that day"
    assert all(h["path"] != "sessions/2026-07-14-s0" for h in hit)          # no loose "2026"/"07" near misses


def test_bulk_rewrite_leaves_activity_dates_alone(wiki):
    wiki._set_today("2026-10-28")
    for p in ("projects/old-open", "notes/no-date-given", "sessions/2026-07-14-s0"):
        text = wiki.store.read(f"{p}.md")
        stripped = "\n".join(line for line in text.splitlines() if not line.startswith(("created:", "date:")))
        wiki.write(p, stripped, "bulk re-index")                             # writer forgot the date fields
    rows = {r["path"]: r for r in wiki.list_pages()}
    assert rows["projects/old-open"]["date"] == "2026-07-15" and rows["projects/old-open"]["updated"] == "2026-10-28"
    assert rows["notes/no-date-given"]["date"] == "2026-07-14"
    assert rows["sessions/2026-07-14-s0"]["date"] == "2026-07-14"


def test_open_projects_before_august(wiki):
    r = wiki.list_view(section="projects", status="In progress", date_to="2026-07-31")
    assert [p["path"] for p in r["pages"]] == ["projects/old-open"]
    open_ = wiki.list_view(section="projects", not_status="Done", sort="date_desc")
    assert [p["path"] for p in open_["pages"]] == ["projects/new-open", "projects/old-open"]
    assert "No pages in that date range" in wiki.list_view(section="projects", date_from="2025-01-01", date_to="2025-01-31")["note"]
    with pytest.raises(WikiError):
        wiki.list_view(date_from="July 1")
    with pytest.raises(WikiError):
        wiki.list_view(sort="random")


def test_appended_section_shows_on_the_day_it_happened(wiki):
    wiki._set_today("2026-07-21")
    wiki.append("projects/old-open", "Shipped.", "ship", heading="Shipped")
    day = wiki.timeline("2026-07-21", "2026-07-21")["days"][0]
    assert {"path": "projects/old-open", "what": "changed"}.items() <= day["items"][0].items()
    assert day["items"][0]["summary"] == "Shipped"
    assert wiki.timeline("2025-01-01", "2025-01-02")["note"].startswith("Nothing dated between")
    with pytest.raises(WikiError):
        wiki.timeline("2026-07-21", "2026-07-01")


def test_log_since_until(wiki):
    wiki._set_today("2026-07-22")
    wiki.write("notes/later-note", page("Later note", "note"), "later")
    entries = wiki.log_view(50, since="2026-07-22", until="2026-07-22")["entries"]
    assert entries and all(e.startswith("## [2026-07-22]") for e in entries) and "notes/later-note" in entries[-1]
    assert len(wiki.log_view(3)["entries"]) == 3


# --- review fixes (2026-10-28): stamping, search fallback, dated events, caps, filters, log truncation ---------------
def raw(w, path, text):
    w.store.write(f"{path}.md", text)           # an existing page written behind the server's back (legacy import)


def test_existing_undated_page_stays_undated_on_write_and_append(wiki):
    wiki._set_today("2026-10-28")
    raw(wiki, "notes/legacy", page("Legacy note", "note"))
    wiki.write("notes/legacy", page("Legacy note", "note", body="Changed body text, still long enough."), "rewrite")
    wiki.append("notes/legacy", "More.", "add")
    row = next(r for r in wiki.list_pages(section="notes") if r["path"] == "notes/legacy")
    assert row["date"] is None and "created:" not in wiki.store.read("notes/legacy.md")
    wiki.write("notes/brand-new", page("Brand new", "note"), "new")                 # near miss: a NEW page is stamped
    assert 'created: "2026-10-28"' in wiki.store.read("notes/brand-new.md")


def test_empty_or_malformed_date_does_not_defeat_the_old_one(wiki):
    wiki._set_today("2026-10-28")
    for bad in ("", "yesterday", "28/10/2026"):
        text = wiki.store.read("notes/no-date-given.md").replace('created: "2026-07-14"', f'created: "{bad}"')
        wiki.write("notes/no-date-given", text, "bad date")
        assert 'created: "2026-07-14"' in wiki.store.read("notes/no-date-given.md"), bad
    text = wiki.store.read("notes/no-date-given.md").replace('created: "2026-07-14"', 'created: "2026-07-01"')
    wiki.write("notes/no-date-given", text, "explicit valid date is allowed")
    assert 'created: "2026-07-01"' in wiki.store.read("notes/no-date-given.md")


def test_malformed_date_on_an_undated_old_page_is_dropped_not_stamped(wiki):
    raw(wiki, "notes/legacy2", page("Legacy two", "note"))
    wiki.write("notes/legacy2", page("Legacy two", "note", created="soon"), "x")
    assert "created:" not in wiki.store.read("notes/legacy2.md")
    wiki.write("notes/fresh-bad", page("Fresh bad", "note", created="soon"), "x")      # new page: dropped, then stamped
    assert 'created: "2026-07-14"' in wiki.store.read("notes/fresh-bad.md")


def test_extra_words_that_match_nothing_fall_back_to_the_date(wiki):
    r = wiki.search_view("what was I doing on 2026-07-20 zebra")
    assert [h["path"] for h in r["results"]] == ["sessions/2026-07-20-later"] and r["results"][0]["snippet"]
    assert r["note"] == "1 pages on 2026-07-20; none also mention zebra"
    assert wiki.search_view("what was I working on around 2026-07-20")["results"][0]["path"] == "sessions/2026-07-20-later"
    assert "note" not in wiki.search_view("what was I working on around 2026-07-20")
    assert wiki.search_view("2026-07-20 later")["results"][0]["path"] == "sessions/2026-07-20-later"      # words match: no note
    assert wiki.search_view("zebra 2026-01-01")["note"].startswith("No entries for 2026-01-01.")        # date itself empty


def test_ingest_style_headings_are_dated_events():
    from mcp_server import dates
    body = "## Update 2026-08-01\n\ntext\n\n## Correction 2026-08-02 - wrong owner\n\n## Notes (2026-08-03)\n\n## Updated plan\n\n## Version 2026-08-045\n"
    got = {(d, k) for d, _, k in dates.events(body)}
    assert got == {("2026-08-01", "changed"), ("2026-08-02", "changed"), ("2026-08-03", "changed")}


def test_bullets_outside_history_are_dated_not_changed(wiki):
    body = "## Notes\n\n- 2026-07-18: met the vendor\n\n## History\n\n- 2026-07-19: status: Open -> Done\n"
    raw(wiki, "notes/mixed", page("Mixed", "note", body=body))

    def first(d):
        return wiki.timeline(d, d)["days"][0]["items"][0]
    assert first("2026-07-18")["what"] == "dated" and first("2026-07-19")["what"] == "changed"


def test_timeline_one_item_per_page_per_day_and_trimmed_created(wiki):
    body = "## History\n\n- 2026-07-18: one\n- 2026-07-18: two\n"
    raw(wiki, "notes/twice", page("Twice", "note", body=body))
    items = wiki.timeline("2026-07-18", "2026-07-18")["days"][0]["items"]
    assert [i["summary"] for i in items] == ["one; two"]
    raw(wiki, "notes/long", page("Long", "note", body="y" * 500, created="2026-07-18"))
    items = wiki.timeline("2026-07-18", "2026-07-18")["days"][0]["items"]
    assert len(next(i for i in items if i["path"] == "notes/long")["summary"]) <= 120


def test_timeline_caps_total_and_keeps_counts(wiki):
    for i in range(40):
        for d in ("2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"):
            raw(wiki, f"notes/n{d[-2:]}-{i}", page(f"N{i} {d}", "note", created=d))
    t = wiki.timeline("2026-08-10", "2026-08-14", per_day=500)
    assert sum(len(d["items"]) for d in t["days"]) == 150 and [d["count"] for d in t["days"]] == [40] * 5
    assert "50 items were left out" in t["note"] and "count is complete" in t["note"]
    small = wiki.timeline("2026-08-10", "2026-08-10")                                  # default 15 per day
    assert len(small["days"][0]["items"]) == 15 and small["days"][0]["count"] == 40
    assert "note" not in wiki.timeline("2026-07-20", "2026-07-20")                     # near miss: under the caps


def test_imported_created_label(wiki):
    raw(wiki, "notes/migrated", page("Migrated", "note", created="2026-07-18", created_source="notion"))
    raw(wiki, "notes/real", page("Real", "note", created="2026-07-18"))
    got = {i["path"]: i["what"] for i in wiki.timeline("2026-07-18", "2026-07-18")["days"][0]["items"]}
    assert got == {"notes/migrated": "created (imported)", "notes/real": "created"}


def test_status_filters_ignore_case_and_limit_is_clamped(wiki):
    exact = [p["path"] for p in wiki.list_view(status="In progress")["pages"]]
    assert exact and [p["path"] for p in wiki.list_view(status="IN PROGRESS")["pages"]] == exact
    rest = [p["path"] for p in wiki.list_view(not_status="done")["pages"]]
    assert all(p["status"].lower() != "done" for p in wiki.list_view(not_status="DONE")["pages"] if "status" in p) and rest
    assert wiki.list_view(status="nonexistent")["pages"] == []                          # near miss
    assert len(wiki.list_view(limit=0)["pages"]) == 1 and len(wiki.list_pages(limit=-5)) == 1
    assert wiki.timeline("2026-07-14", "2026-07-14", per_day=0)["days"][0]["items"]


def test_sort_path_sorts_by_path(wiki):
    paths = [r["path"] for r in wiki.list_pages(sort="path")]
    assert paths == sorted(paths) and len(paths) > 3


def test_log_range_returns_newest_n_and_reports_truncation(wiki):
    wiki._set_today("2026-07-30")
    for i in range(5):
        wiki.write(f"notes/log{i}", page(f"Log {i}", "note"), f"w{i}")
    r = wiki.log_view(2, since="2026-07-30", until="2026-07-30")
    assert r["truncated"] is True and [e.split("|")[1].split()[0] for e in r["entries"]] == ["notes/log3", "notes/log4"]
    full = wiki.log_view(50, since="2026-07-30", until="2026-07-30")
    assert len(full["entries"]) == 5 and full["truncated"] is False
    assert wiki.log_view(2, since="", until="")["truncated"] is False and len(wiki.log_view(2, since="", until="")["entries"]) == 2


def test_search_stopwords_drop_question_filler(wiki):
    from mcp_server.wiki_store import STOPWORDS
    assert {"doing", "working", "worked", "on", "around", "during", "happen", "happened"} <= STOPWORDS
    assert "later" not in STOPWORDS


def test_context_with_a_date_is_an_exact_lookup(wiki):
    r = wiki.context("what happened on 2026-07-20")
    assert [h["path"] for h in r["results"]] == ["sessions/2026-07-20-later"] and "neighbours" in r["results"][0]
    empty = wiki.context("2026-01-01")
    assert empty["results"] == [] and empty["note"].startswith("No entries for 2026-01-01.")
    assert wiki.context("later")["results"]                                        # near miss: no date, loose matching


@pytest.mark.parametrize("sid,expected", [
    ("raw/claude-memory/2026-10-27/profile.md", "2026-10-27"),
    ("private:personal-directory:2026-10-27", "2026-10-27"),
    ("claude-session:2026-10-28", "2026-10-28"),
    ("0123456789abcdef0123456789abcdef", None),             # bare Notion id
    ("x:notadate", None),
    ("raw/claude-memory/profile.md", None),
    ("abc2026-10-27", None),
])
def test_dated_source_ids(sid, expected):
    from mcp_server import dates
    assert dates.page_date({"source_ids": [sid]}) == expected


def test_meta_caches_date_and_events(wiki):
    m = wiki._meta("sessions/2026-07-20-later.md")
    assert m["date"] == "2026-07-20" and wiki._meta("sessions/2026-07-20-later.md") is m
