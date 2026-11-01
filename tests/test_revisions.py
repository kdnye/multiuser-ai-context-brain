"""Optimistic concurrency on wiki_write (roadmap item 1, 2026-10-29; pattern from GBrain's expected_revision)."""

from __future__ import annotations

import pytest

from mcp_server.store import CachedStore
from mcp_server.wiki_store import Wiki, WikiError
from tests.test_wiki_relations import NOTE, Mem


@pytest.fixture
def wiki():
    w = Wiki(CachedStore(Mem()), today=lambda: "2026-10-29")
    w.write("notes/plan", NOTE.format(t="Plan", extra="", body="First version of the plan."), "seed")
    return w


def test_a_stale_rewrite_is_refused_and_names_the_current_revision(wiki):
    seen = wiki.revision("notes/plan")
    wiki.append("notes/plan", "Someone else added this.", "other client", "Update")      # a second client
    with pytest.raises(WikiError, match=r"revision_conflict: notes/plan changed since you read it \(current revision [0-9a-f]{12}\)"):
        wiki.write("notes/plan", NOTE.format(t="Plan", extra="", body="My rewrite."), "stale rewrite", expected_revision=seen)
    assert "Someone else added this." in wiki.read("notes/plan")                           # nothing was lost


def test_a_current_revision_writes_and_returns_the_new_one(wiki):
    seen = wiki.revision("notes/plan")
    r = wiki.write("notes/plan", NOTE.format(t="Plan", extra="", body="Second version."), "rewrite", expected_revision=seen)
    assert r["revision"] == wiki.revision("notes/plan") != seen
    assert wiki.write("notes/plan", NOTE.format(t="Plan", extra="", body="Third."), "no check")["action"] == "updated"


def test_expected_revision_on_a_missing_page_is_a_conflict(wiki):
    with pytest.raises(WikiError, match="does not exist"):
        wiki.write("notes/new", NOTE.format(t="New", extra="", body="Body text."), "x", expected_revision="abc")
    assert wiki.revision("notes/nope") is None
