"""Regression tests for the code-review findings on PR #1 (one test per finding)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from mcp_server.store import CachedStore
from mcp_server.wiki_store import MAX_BYTES, Wiki, WikiError
from tests.test_store import FakeBackend
from tools.wiki.scrub import scrub
from tools.wiki.sync import pull, verify

NOTE = '---\ntitle: "{t}"\ntype: "{ty}"\nscope: "work"\nsource_ids: ["x"]\n{extra}---\n\n# {t}\n\n{body}\n'


@pytest.fixture
def wiki(tmp_path):
    return Wiki(tmp_path / "w", today=lambda: "2026-11-01")


def session(**extra):
    return {"session": {"date": "2026-11-01", "summary": "Did things.", "concerning": "Widgets"}, **extra}


def test_a_failing_ingest_writes_nothing(wiki):
    before = sorted(wiki.store.names())
    with pytest.raises(WikiError):
        wiki.ingest_session(session(projects=[{"name": "Fine project"}], notes=[{"title": "Huge", "body": "x" * (MAX_BYTES + 10)}]))
    assert sorted(wiki.store.names()) == before


def test_notes_with_the_same_slug_stay_separate(wiki):
    wiki.ingest_session(session(notes=[{"title": "C++", "body": "Templates."}]))
    r = wiki.ingest_session(session(notes=[{"title": "C#", "body": "Generics."}]))
    assert "notes/c-2" in r["created"]
    assert "Generics" not in wiki.read("notes/c") and 'title: "C#"' in wiki.read("notes/c-2")


def test_concurrent_appends_keep_both(wiki):
    wiki.write("notes/log", NOTE.format(t="Log", ty="note", extra="", body="Start."), "s")
    threads = [threading.Thread(target=wiki.append, args=("notes/log", f"entry {i}", "s")) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    text = wiki.read("notes/log")
    assert all(f"entry {i}" in text for i in range(8))


def test_area_hubs_with_colliding_slugs_both_exist(wiki):
    for slug, area in (("a", "R&D"), ("b", "R D")):
        wiki.write(f"projects/{slug}", NOTE.format(t=slug.upper(), ty="project", extra=f'area: "{area}"\n', body="x"), "s")
    hubs = {n for n in wiki.store.names() if n.startswith("reference/area-r-d")}
    assert hubs == {"reference/area-r-d.md", "reference/area-r-d-2.md"}


def test_a_multiline_summary_cannot_forge_log_entries(wiki):
    wiki.write("notes/x", NOTE.format(t="X", ty="note", extra="", body="x"), "ok\n## [2000-01-01] create | notes/fake — fake")
    assert "\n## [2000-01-01]" not in wiki.store.read("log.md")


@pytest.mark.parametrize("sid", ["john@example.com", "123-45-6789", "555-555-0142"])
def test_sensitive_source_ids_are_refused(wiki, sid):
    with pytest.raises(WikiError, match="source id"):
        wiki.write("notes/x", NOTE.format(t="X", ty="note", extra="", body="x").replace('["x"]', f'["{sid}"]'), "s")


def test_parenthesised_phone_without_a_space_is_scrubbed():
    assert scrub("call (212)555-1212 now")[0] == "call {phone} now"


def test_pull_refuses_object_names_outside_the_destination(tmp_path):
    b = FakeBackend()
    b.put("../escape.md", "x")
    with pytest.raises(ValueError, match="outside"):
        pull(b, tmp_path / "dest")
    assert not (tmp_path / "escape.md").exists()


def test_task_update_refuses_the_other_record_types_fields(wiki):
    wiki.ingest_session(session(tasks=[{"task": "Ship it"}], future_needs=[{"name": "Later thing"}]))
    with pytest.raises(WikiError, match="horizon"):
        wiki.update_record("tasks/ship-it", horizon="Now")
    with pytest.raises(WikiError, match="priority"):
        wiki.update_record("future-needs/later-thing", priority="High")


def test_due_with_a_trailing_newline_is_refused(wiki):
    wiki.ingest_session(session(tasks=[{"task": "Ship it"}]))
    with pytest.raises(WikiError, match="due"):
        wiki.update_record("tasks/ship-it", due="2026-01-01\n")


def test_scrub_counts_cover_every_ingested_page(wiki):
    r = wiki.ingest_session(session(notes=[{"title": "Contact", "body": "Mail pat@example.com."}]))
    assert r["scrubbed"].get("email") == 1


def test_ingest_reports_unknown_fields_and_refuses_bad_dates_and_missing_titles(wiki):
    r = wiki.ingest_session(session(notes=[{"title": "Typo", "bdy": "lost"}]))
    assert r["ignored_fields"] == ["notes[0].bdy"]
    with pytest.raises(WikiError, match="session.date"):
        wiki.ingest_session({"session": {"date": "not-a-date", "summary": "s"}})
    with pytest.raises(WikiError, match="needs a title"):
        wiki.ingest_session(session(notes=[{"body": "no title"}]))
    assert wiki.ingest_session({**session(), "repos": [], "session": {**session()["session"], "store_raw_json": True}})["ignored_fields"] == []


def test_verify_fails_on_extra_pages_but_ignores_dot_folders(tmp_path):
    b = FakeBackend()
    b.put("notes/a.md", "A\n")
    pull(b, tmp_path / "d")
    (tmp_path / "d" / ".obsidian").mkdir()
    (tmp_path / "d" / ".obsidian" / "x.md").write_text("plugin", encoding="utf-8")
    assert verify(tmp_path / "d")["ok"]
    (tmp_path / "d" / "notes" / "stale.md").write_text("old", encoding="utf-8")
    assert verify(tmp_path / "d")["extra"] == ["notes/stale.md"] and not verify(tmp_path / "d")["ok"]


def test_lint_reports_a_malformed_review_date(tmp_path):
    w = Wiki(tmp_path / "w", today=lambda: "2026-11-01")
    w.write("how-to/x", NOTE.format(t="X", ty="how-to", extra='owner: "it"\nreview_by: "2027-01-01"\n', body="x"), "s")
    f = tmp_path / "w" / "how-to" / "x.md"                                  # edited on disk, bypassing write validation
    f.write_text(f.read_text(encoding="utf-8").replace('review_by: "2027-01-01"', 'review_by: "soon"'), encoding="utf-8")
    assert Wiki(CachedStore(_Dir(tmp_path / "w")), today=lambda: "2026-11-01").lint()["by_kind"].get("bad_review_by") == 1


class _Dir:
    def __init__(self, root):
        self.root = Path(root)

    def list_names(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*.md"))

    def get(self, name):
        f = self.root / name
        return f.read_text(encoding="utf-8") if f.is_file() else None

    def put(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")
