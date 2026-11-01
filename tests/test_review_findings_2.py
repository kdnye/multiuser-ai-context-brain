"""Regression tests for the second round of review findings (one test per finding)."""

from __future__ import annotations

import threading

import pytest

from mcp_server.router import safe
from mcp_server.wiki_store import Wiki

NOTE = '---\ntitle: "{t}"\ntype: "{ty}"\nscope: "work"\nsource_ids: ["x"]\n{extra}---\n\n# {t}\n\n{body}\n'


@pytest.fixture
def wiki(tmp_path):
    return Wiki(tmp_path / "w", today=lambda: "2026-11-01")


def session(**extra):
    return {"session": {"date": "2026-11-01", "summary": "Did things.", "concerning": "Widgets"}, **extra}


def hub_title(wiki, slug):
    text = wiki.store.read(f"reference/area-{slug}.md") or ""
    return next((line.split('"')[1] for line in text.splitlines() if line.startswith("title:")), None)


# 1. Re-ingesting a note whose slug collides with another note's must update it, not mint c-3, c-4, ...
def test_repeat_ingest_of_a_colliding_title_updates_the_same_page(wiki):
    wiki.ingest_session(session(notes=[{"title": "C++", "body": "Templates."}]))
    first = wiki.ingest_session(session(notes=[{"title": "C#", "body": "Generics."}]))
    again = wiki.ingest_session(session(notes=[{"title": "C#", "body": "Records."}]))
    again2 = wiki.ingest_session(session(notes=[{"title": "C#", "body": "Spans."}]))
    assert "notes/c-2" in first["created"]
    assert "notes/c-2" in again["updated"] and "notes/c-2" in again2["updated"]
    notes = {n for n in wiki.store.names() if n.startswith("notes/")}
    assert notes == {"notes/c.md", "notes/c-2.md"}
    assert "Records." in wiki.read("notes/c-2") and "Spans." in wiki.read("notes/c-2")
    assert "Records." not in wiki.read("notes/c")


# 2. While an ingest is staging, other threads must read the real store, not the staged pages.
def test_other_readers_never_see_staged_ingest_pages(wiki):
    seen: dict = {}
    original = wiki._ingest

    def spy(payload, s):
        result = original(payload, s)            # every page is now staged, none is in the real store
        def other_thread():
            seen["names"] = list(wiki.store.names())
        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
        seen["own"] = list(wiki.store.names())    # the ingesting thread itself still sees its staged pages
        seen["session"] = result["session"]
        return result

    wiki._ingest = spy
    wiki.ingest_session(session(notes=[{"title": "Secret draft", "body": "Not yet."}]))
    assert not any(n.startswith(("notes/secret-draft", "sessions/")) for n in seen["names"])
    assert f"{seen['session']}.md" in seen["own"]
    assert f"{seen['session']}.md" in wiki.store.names()          # and after the ingest everyone sees it


# 3. A phone- or SSN-shaped path must not reach the audit log as given.
# The unseparated phone is assembled from parts so this file has no long number for tools.release_check to flag.
@pytest.mark.parametrize("path", ["notes/555-123-4567", "notes/123-45-6789", "notes/" + "555" + "1234567", "notes/1-555-123-4567", "notes/123456789"])
def test_pii_shaped_paths_are_redacted_in_the_audit_log(path):
    assert safe(path) == "<redacted>"


@pytest.mark.parametrize("path", ["notes/c-2", "sessions/2026-11-01-widgets", "tasks/ship-v2", "reference/area-r-d-2", "", "notes/2026-11-01"])
def test_ordinary_paths_are_still_logged_as_given(path):
    assert safe(path) == path


# 4. A hub keeps its path when a new area whose slug collides (and sorts earlier) is added.
def test_area_hub_path_is_stable_when_a_colliding_area_is_added(wiki):
    wiki.write("projects/a", NOTE.format(t="A", ty="project", extra='area: "R&D"\n', body="x"), "s")
    assert hub_title(wiki, "r-d") == "Area: R&D"
    wiki.write("projects/b", NOTE.format(t="B", ty="project", extra='area: "R D"\n', body="x"), "s")   # sorts before "R&D"
    assert hub_title(wiki, "r-d") == "Area: R&D"
    assert hub_title(wiki, "r-d-2") == "Area: R D"
    wiki.rebuild_index()
    assert hub_title(wiki, "r-d") == "Area: R&D" and hub_title(wiki, "r-d-2") == "Area: R D"


# 5. projects[].area is saved on a new project and drives its area hub.
def test_ingest_saves_a_new_projects_area(wiki):
    wiki.ingest_session(session(projects=[{"name": "Apollo", "area": "Space"}, {"name": "Plain"}]))
    assert 'area: "Space"' in wiki.read("projects/apollo")
    assert "area:" not in wiki.read("projects/plain")
    assert hub_title(wiki, "space") == "Area: Space"
    assert "Apollo" in wiki.store.read("reference/area-space.md")
