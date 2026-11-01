"""Tests for mcp_server.wiki_store (the logic behind the MCP tools)."""

from __future__ import annotations

import pytest

from mcp_server.wiki_store import Wiki, WikiError
from tools.wiki.lint import parse_frontmatter

PAGE = '---\ntitle: "{t}"\ntype: "note"\nscope: "work"\nsource_ids: ["x"]\nupdated: "2026-01-01"\n---\n# {t}\n\n{body}\n'


@pytest.fixture
def wiki(tmp_path):
    for d in ("projects", "notes"):
        (tmp_path / d).mkdir()
    (tmp_path / "projects" / "widgets.md").write_text(
        PAGE.format(t="WIDGETS", body="Widget ordering tool on Cloud Run.").replace('"note"', '"project"'), encoding="utf-8")
    (tmp_path / "notes" / "cloud-run-rot.md").write_text(PAGE.format(t="Cloud Run image rot", body="Old images expire; rebuild them. Cloud Run jobs rot."), encoding="utf-8")
    (tmp_path / "notes" / "other.md").write_text(PAGE.format(t="Unrelated", body="Nothing here about hosting."), encoding="utf-8")
    (tmp_path / "log.md").write_text("---\ntitle: \"Log\"\ntype: \"log\"\n---\n\n# Log\n\n## [2026-10-26] ingest | first\n", encoding="utf-8")
    w = Wiki(tmp_path, today=lambda: "2026-10-27")
    w.rebuild_index()
    return w


@pytest.mark.parametrize("bad", ["../secrets", "/etc/passwd", "notes/../../x", "Notes/Upper", "random/thing", "notes/", "notes/a b", "notes/.hidden", ""])
def test_bad_paths_are_refused(wiki, bad):
    with pytest.raises(WikiError):
        wiki.read(bad)
    with pytest.raises(WikiError):
        wiki.write(bad, PAGE.format(t="T", body="b"), "s")


def test_read_accepts_md_suffix_and_special_pages(wiki):
    assert "WIDGETS" in wiki.read("projects/widgets.md")
    assert "# Index" in wiki.read("index")
    assert "## [2026-10-26]" in wiki.read("log")
    with pytest.raises(WikiError, match="no such page"):
        wiki.read("notes/missing")


def test_list_filters_by_section_and_scope(wiki):
    assert {p["path"] for p in wiki.list_pages(section="notes")} == {"notes/cloud-run-rot", "notes/other"}
    assert [p["title"] for p in wiki.list_pages(section="projects")] == ["WIDGETS"]
    assert wiki.list_pages(scope="personal") == []
    assert len(wiki.list_pages(limit=1)) == 1
    capped = wiki.list_view(section="notes", limit=1)
    assert len(capped["pages"]) == 1 and capped["truncated"] is True and "raise limit" in capped["hint"]
    assert wiki.list_view(section="notes", limit=50) == {"pages": wiki.list_pages(section="notes"), "truncated": False}


def test_search_ranks_title_hits_first_and_falls_back_to_any_term(wiki):
    hits = wiki.search("cloud run")
    assert hits[0]["path"] == "notes/cloud-run-rot" and "2/2" in hits[0]["matched"]
    assert any(h["path"] == "projects/widgets" for h in hits)
    assert "cloud" in hits[0]["snippet"].lower() or "run" in hits[0]["snippet"].lower()
    loose = wiki.search("cloud zzzz")           # one term matches: still returned, flagged partial
    assert loose and loose[0]["matched"] == "1/2 terms"
    assert wiki.search("qqqqqq") == []
    with pytest.raises(WikiError):
        wiki.search("!")


def test_write_validates_and_refuses_special_files(wiki):
    ok = PAGE.format(t="New", body="Body text here.")
    with pytest.raises(WikiError, match="frontmatter"):
        wiki.write("notes/new", "# no frontmatter", "s")
    with pytest.raises(WikiError, match="missing"):
        wiki.write("notes/new", '---\ntitle: "x"\n---\nbody', "s")
    with pytest.raises(WikiError, match="scope"):
        wiki.write("notes/new", ok.replace('"work"', '"weird"'), "s")
    with pytest.raises(WikiError, match="source_ids"):
        wiki.write("notes/new", ok.replace('["x"]', '"x"'), "s")
    with pytest.raises(WikiError, match="summary"):
        wiki.write("notes/new", ok, "  ")
    with pytest.raises(WikiError, match="maintained automatically"):
        wiki.write("index", ok, "s")
    with pytest.raises(WikiError, match="too large"):
        wiki.write("notes/new", ok + "x" * 210_000, "s")


def test_write_creates_scrubs_stamps_indexes_and_logs(wiki):
    page = PAGE.format(t="Mail rules", body="Ask pat.smith@example.com or accounting@example.com. See [[notes/missing-page]].")
    r = wiki.write("notes/mail-rules", page, "capture mail rules")
    assert r["action"] == "created" and r["scrubbed"] == {"email": 1}
    assert r["warnings"] == ["link to a page that does not exist: [[notes/missing-page]]"]
    text = wiki.read("notes/mail-rules")
    assert "pat.smith@example.com" not in text and "{email}" in text and "accounting@example.com" in text
    assert parse_frontmatter(text)["updated"] == "2026-10-27"               # always stamped with today
    assert "[[notes/mail-rules|Mail rules]]" in wiki.read("index")
    assert wiki.log_tail(1) == ["## [2026-10-27] create | notes/mail-rules — capture mail rules"]
    again = wiki.write("notes/mail-rules", page, "tweak")
    assert again["action"] == "updated" and wiki.log_tail(1)[0].startswith("## [2026-10-27] update | notes/mail-rules")


def test_index_view_is_a_small_table_of_contents_or_one_section(wiki):
    toc = wiki.index_view()
    assert "Notes (2)" in toc and "Projects (1)" in toc and "wiki_index(section=" in toc
    assert "Cloud Run image rot" not in toc and len(toc) < len(wiki.read("index")) + 400
    assert "[[notes/cloud-run-rot|Cloud Run image rot]]" in wiki.index_view("notes")
    assert wiki.index_view("Notes") == wiki.index_view("notes")
    first = wiki.index_view("notes", limit=1)
    assert first.count("\n- ") == 1 and "call again with offset=1" in first
    assert "call again" not in wiki.index_view("notes", offset=1, limit=1)
    with pytest.raises(WikiError, match="no such section"):
        wiki.index_view("nope")


def test_lint_reports_through_the_store(wiki):
    result = wiki.lint()
    assert result["pages"] >= 3 and "by_kind" in result


PAYLOAD = {
    "meta": {"source": "Claude"},
    "session": {"title": "Daily chat on 2026-10-27 concerning WIDGETS", "date": "2026-10-27", "summary": "Fixed the fee builder.",
                "concerning": "WIDGETS", "cross_related_projects": ["Brand New Project"], "source_ai": "Claude"},
    "projects": [{"name": "Brand New Project", "summary": "A new thing.", "type": "Personal", "status": "In progress"}],
    "notes": [{"title": "Fee builder rules", "tag": ["Reference"], "project": "WIDGETS", "body": "Rules go here. Mail ops@example.com."}],
    "tasks": [{"task": "Merge PR", "project": "WIDGETS", "priority": "High"}],
    "future_needs": [{"name": "Tiered fees", "horizon": "Later"}],
}


def test_ingest_session_creates_pages_links_and_keeps_follow_ups(wiki):
    r = wiki.ingest_session(PAYLOAD)
    assert r["session"] == "sessions/2026-10-27-widgets"
    assert set(r["created"]) == {"projects/brand-new-project", "notes/fee-builder-rules", "sessions/2026-10-27-widgets",
                                 "tasks/merge-pr", "future-needs/tiered-fees"}          # tasks and needs are records
    assert r["follow_ups"]["tasks"] == 1 and r["follow_ups"]["future_needs"] == 1
    session = wiki.read(r["session"])
    assert "[[projects/widgets|WIDGETS]]" in session and "[[projects/brand-new-project|Brand New Project]]" in session
    assert "[[tasks/merge-pr|Merge PR]]" in session and "[[future-needs/tiered-fees|Tiered fees]]" in session
    assert "## Tasks (live: 1 open, 0 done)" in session                       # live list computed on read
    assert parse_frontmatter(session)["scope"] == "work"                       # inherited from the WIDGETS page
    assert parse_frontmatter(wiki.read("projects/brand-new-project"))["scope"] == "personal"
    note = wiki.read("notes/fee-builder-rules")
    assert "[[projects/widgets|WIDGETS]]" in note and "{email}" not in note and "ops@example.com" in note   # role mailbox kept
    assert wiki.lint()["by_kind"].get("broken_link") is None


def test_ingesting_the_same_note_again_appends_an_update_and_the_session_gets_a_new_page(wiki):
    wiki.ingest_session(PAYLOAD)
    r = wiki.ingest_session(PAYLOAD)
    assert r["session"] == "sessions/2026-10-27-widgets-2"
    assert r["updated"] == ["notes/fee-builder-rules", "tasks/merge-pr", "future-needs/tiered-fees"]   # deduped by title
    assert "## Update 2026-10-27" in wiki.read("notes/fee-builder-rules")
    assert "projects/brand-new-project" not in r["created"]                   # project already exists


def test_ingest_requires_a_summary(wiki):
    with pytest.raises(WikiError, match="summary"):
        wiki.ingest_session({"session": {"title": "t"}})


def test_meta_cache_sees_rewrites_through_a_cached_store():
    # The parse cache is keyed on the text object; a write through CachedStore must invalidate it.
    from mcp_server.store import CachedStore

    class Mem:
        def __init__(self):
            self.d = {}

        def list_names(self):
            return list(self.d)

        def get(self, n):
            return self.d.get(n)

        def put(self, n, t):
            self.d[n] = t

    w = Wiki(CachedStore(Mem()), today=lambda: "2026-10-27")
    page = '---\ntitle: "Alpha"\ntype: "note"\nscope: "personal"\nsource_ids: ["t"]\n---\n\nfirst version zebra\n'
    w.write("notes/alpha", page, "create")
    assert w.search("zebra")[0]["path"] == "notes/alpha"
    w.write("notes/alpha", page.replace("zebra", "walrus"), "edit")
    assert w.search("walrus")[0]["path"] == "notes/alpha"
    assert w.search("zebra") == []
    assert "walrus" in w.index_view("notes")


RECORD = ('---\ntitle: "{t}"\ntype: "{ty}"\nscope: "work"\nsource_ids: ["x"]\n{extra}project: "projects/widgets"\n'
          'updated: "2026-01-01"\n---\n# {t}\n\nBody.\n')


def test_tasks_and_future_needs_are_records(wiki):
    task = RECORD.format(t="Rotate a secret", ty="task", extra='status: "Not started"\npriority: "High"\n')
    need = RECORD.format(t="Alert on failures", ty="future_need", extra='horizon: "Later"\nstatus: "Open"\n')
    wiki.write("tasks/rotate-a-secret", task, "add task")
    wiki.write("future-needs/alert-on-failures", need, "add future need")
    index = wiki.read("index")
    assert "## Tasks (1)" in index and "## Future-needs (1)" in index
    assert [p["path"] for p in wiki.list_pages(section="tasks")] == ["tasks/rotate-a-secret"]
    assert "## Tasks (live: 1 open, 0 done)" in wiki.read("projects/widgets")      # project page shows its records live
    assert "Rotate a secret" in wiki.read("reference/tasks")                      # generated rollup hub
    r = wiki.update_record("tasks/rotate-a-secret", status="Done", note="rotated")
    assert r["changes"] == ["status: Not started -> Done"]
    assert "## History" in wiki.read("tasks/rotate-a-secret")
    assert "0 open, 1 done" in wiki.read("projects/widgets")


def test_records_keep_their_original_date_for_timeline_lookups(wiki):
    task = RECORD.format(t="Call vendor", ty="task", extra='status: "Not started"\ncreated: "2026-07-09"\n')
    wiki.write("tasks/call-vendor", task, "copy from the second brain")
    wiki.update_record("tasks/call-vendor", status="Done")
    then = wiki.timeline("2026-07-09", "2026-07-09")["days"][0]["items"]
    assert [i["path"] for i in then] == ["tasks/call-vendor"] and then[0]["what"] == "created"      # the day it was made
    now = wiki.timeline("2026-10-27", "2026-10-27")["days"][0]["items"]
    assert now[0]["what"] == "changed" and "Done" in now[0]["summary"]                              # the day it was closed


def test_record_values_are_validated_and_hub_is_generated(wiki):
    bad = RECORD.format(t="Bad", ty="task", extra='status: "Open"\n')
    with pytest.raises(WikiError):
        wiki.write("tasks/bad", bad, "bad status")
    with pytest.raises(WikiError):
        wiki.update_record("projects/widgets", status="Done")                      # not a record
    with pytest.raises(WikiError):
        wiki.write("reference/tasks", bad, "the hub is generated")


PERSON = ('---\ntitle: "Pat Example"\ntype: "person"\nscope: "work"\nsource_ids: ["x"]\nemails: ["pat@example.com"]\n'
          'phone: "555-555-0142"\n---\n# Pat Example\n\nMobile 555-555-0142, email pat@example.com, SSN 123-45-6789.\n')


@pytest.mark.parametrize("personal, kept", [(True, True), (False, False)])
def test_phone_numbers_survive_only_on_person_pages_in_a_personal_silo(tmp_path, personal, kept):
    w = Wiki(tmp_path, today=lambda: "2026-10-30", personal=personal)
    w.write("people/pat-example", PERSON, "add person")
    text = w.read("people/pat-example")
    assert ("555-555-0142" in text) is kept and "{phone}" not in text if kept else "{phone}" in text
    assert "pat@example.com" in text                       # person pages keep email in every silo (existing rule)
    assert "123-45-6789" not in text and "{ssn}" in text   # IDs are removed everywhere


def test_phone_numbers_are_still_removed_from_other_pages_in_a_personal_silo(tmp_path):
    w = Wiki(tmp_path, today=lambda: "2026-10-30", personal=True)
    note = PAGE.format(t="Call log", body="Call 555-555-0142 about the order.")
    w.write("notes/call-log", note, "add note")
    assert "555-555-0142" not in w.read("notes/call-log") and "{phone}" in w.read("notes/call-log")


@pytest.mark.parametrize("personal, phrase", [(True, "phone numbers"), (False, "phones and IDs stay in Contacts")])
def test_people_hub_wording_matches_the_phone_rule(tmp_path, personal, phrase):
    w = Wiki(tmp_path, today=lambda: "2026-10-30", personal=personal)
    w.write("people/pat-example", PERSON, "add person")
    assert phrase in w.read("reference/people")


@pytest.mark.parametrize("bad", [
    {**PAYLOAD, "tasks": [{"task": ["not", "a", "string"]}]},
    {**PAYLOAD, "future_needs": "Tiered fees"},
    {**PAYLOAD, "notes": [{"title": "x", "body": 5}]},
    {**PAYLOAD, "tasks": [{"task": f"t{i}"} for i in range(201)]},
    {**PAYLOAD, "session": {**PAYLOAD["session"], "cross_related_projects": [1]}},
])
def test_malformed_payload_is_refused_before_anything_is_written(wiki, bad):
    before = sorted(wiki.store.names())
    with pytest.raises(WikiError):
        wiki.ingest_session(bad)
    assert sorted(wiki.store.names()) == before
