"""Relationality, recall, append and batched ingest (the 'second natural context' layer)."""

from __future__ import annotations

import pytest

from mcp_server.store import CachedStore
from mcp_server.wiki_store import Wiki, WikiError
from tools.wiki.lint import lint_pages

NOTE = '---\ntitle: "{t}"\ntype: "note"\nscope: "work"\nsource_ids: ["x"]\n{extra}---\n\n# {t}\n\n{body}\n'


class Mem:
    """A fake bucket that counts writes per object, so batching is observable."""

    def __init__(self):
        self.d, self.puts = {}, {}

    def list_names(self):
        return list(self.d)

    def get(self, n):
        return self.d.get(n)

    def put(self, n, t):
        self.d[n] = t
        self.puts[n] = self.puts.get(n, 0) + 1


@pytest.fixture
def wiki():
    w = Wiki(CachedStore(Mem()), today=lambda: "2026-10-28")
    w.write("projects/widgets", NOTE.format(t="WIDGETS", extra="", body="Widget ordering tool.").replace('"note"', '"project"'), "seed")
    w.write("notes/old-release", NOTE.format(t="Old release flow", extra="", body="Push to main deploys prod."), "seed")
    w.write("notes/two-stage-release", NOTE.format(
        t="WIDGETS two-stage release", extra='project: "projects/widgets"\nsupersedes: ["notes/old-release"]\n',
        body="Releases follow the team checklist. See [[projects/widgets|WIDGETS]]."), "seed")
    return w


def test_links_show_outgoing_backlinks_and_typed_relations_both_ways(wiki):
    two = wiki.links("notes/two-stage-release")
    assert {(o["path"], o["via"]) for o in two["outgoing"]} == {("projects/widgets", "link"), ("projects/widgets", "project"), ("notes/old-release", "supersedes")}
    old = wiki.links("notes/old-release")
    assert old["backlinks"] == [{"path": "notes/two-stage-release", "title": "WIDGETS two-stage release", "via": "supersedes"}]
    proj = wiki.links("projects/widgets")
    assert proj["backlinks"][0]["via"] == "project,link" and proj["missing_targets"] == []
    with pytest.raises(WikiError):
        wiki.links("notes/nope")


def test_write_warns_about_relations_to_missing_pages(wiki):
    r = wiki.write("notes/x", NOTE.format(t="X", extra='related: ["notes/ghost"]\n', body="Body text long enough."), "x")
    assert any("related points to a page that does not exist: notes/ghost" in w for w in r["warnings"])


def test_context_returns_hits_with_neighbours_and_the_profile_pointer(wiki):
    c = wiki.context("two-stage release")
    assert c["results"][0]["path"] == "notes/two-stage-release"
    assert {n["path"] for n in c["results"][0]["neighbours"]} >= {"projects/widgets", "notes/old-release"}
    assert c["profile"] == "reference/profile" and "wiki_read" in c["hint"]
    assert "nothing on this" in wiki.context("zzzz unmatched qqqq")["hint"]


def test_append_adds_a_dated_section_and_keeps_frontmatter(wiki):
    r = wiki.append("projects/widgets", "Fee builder merged.", "fee builder status", heading="Status")
    assert r["action"] == "updated"
    page = wiki.read("projects/widgets")
    assert page.startswith("---\ntitle: \"WIDGETS\"") and "## Status (2026-10-28)\n\nFee builder merged." in page
    assert wiki.search("fee builder")[0]["path"] == "projects/widgets"
    assert "fee builder status" in wiki.store.read("log.md")
    for bad in (("notes/missing", "t", "s"), ("projects/widgets", "  ", "s"), ("projects/widgets", "t", " ")):
        with pytest.raises(WikiError):
            wiki.append(*bad)


def test_append_is_scrubbed_like_any_write(wiki):
    r = wiki.append("projects/widgets", "Call 555-555-0142 or mail someone@example.com", "contact")
    assert r["scrubbed"] == {"email": 1, "phone": 1}
    assert "555-0142" not in wiki.read("projects/widgets")


def test_ingest_rebuilds_index_and_log_once_and_lists_pages_to_review(wiki):
    store = wiki.store._backend
    before = (store.puts.get("index.md", 0), store.puts.get("log.md", 0))
    r = wiki.ingest_session({"session": {"title": "Release work", "date": "2026-10-28", "summary": "Worked on the WIDGETS release process.",
                                         "concerning": "WIDGETS"},
                             "notes": [{"title": "Release checklist", "body": "Tag after test passes.", "project": "WIDGETS"},
                                       {"title": "Tagging rules", "body": "Tag every release.", "project": "WIDGETS"}]})
    after = (store.puts["index.md"], store.puts["log.md"])
    assert after == (before[0] + 1, before[1] + 1)          # three pages written, one index rebuild, one log write
    log = store.d["log.md"]
    assert log.count("new note from session 2026-10-28") == 2 and "session 2026-10-28" in log
    review = {x["path"] for x in r["pages_to_review"]}
    assert "notes/two-stage-release" in review or "projects/widgets" in review
    assert not review & ({r["session"]} | set(r["created"]))
    assert "notes/release-checklist" in wiki.store.read("index.md")


def test_lint_counts_typed_relations_and_flags_broken_ones():
    texts = {
        "index.md": "[[notes/a]] [[notes/b]]",
        "notes/a.md": NOTE.format(t="A", extra='related: "notes/b"\n', body="Body of page A, long enough."),
        "notes/b.md": NOTE.format(t="B", extra='supersedes: ["notes/ghost"]\n', body="Body of page B, long enough."),
    }
    r = lint_pages(texts)
    kinds = {(i["kind"], i.get("page")) for i in r["issues"]}
    assert ("orphan", "notes/b.md") not in kinds           # reached through a typed relation only
    assert ("broken_relation", "notes/b.md") in kinds


def test_map_and_area_hubs_are_generated_linked_and_protected(wiki):
    start = wiki.read("reference/start-here")
    assert "[[reference/area-internal-apps|Internal Apps]]" not in start            # fixture project has no area
    assert "[[reference/area-unsorted|Unsorted]]: 1 projects" in start
    hub = wiki.read("reference/area-unsorted")
    assert "[[projects/widgets|WIDGETS]]" in hub and "1 linked pages" in hub    # one page (two-stage-release) links it, counted once
    with pytest.raises(WikiError):
        wiki.write("reference/start-here", NOTE.format(t="x", extra="", body="hand edit attempt"), "x")
    with pytest.raises(WikiError):
        wiki.append("reference/area-unsorted", "x", "x")
    assert all(not p["path"].startswith("reference/") for p in wiki.list_pages())
    assert all(not h["path"].startswith("reference/") for h in wiki.search("widgets"))


def test_hubs_are_rewritten_only_when_their_content_changes(wiki):
    store = wiki.store._backend
    n = store.puts["reference/start-here.md"]
    wiki.append("notes/old-release", "Still documented for history.", "history note")
    assert store.puts["reference/start-here.md"] == n                       # nothing on the map changed
    wiki.write("projects/gadgets", NOTE.format(t="GADGETS", extra='area: "Internal Apps"\nstatus: "In progress"\n', body="Expense tool.").replace('"note"', '"project"'), "new project")
    assert store.puts["reference/start-here.md"] == n + 1
    assert "1 projects, 1 active" in wiki.read("reference/start-here") and "Active (1)" in wiki.read("reference/area-internal-apps")


def test_generated_hubs_are_listed_in_the_index(wiki):
    index = wiki.store.read("index.md")
    assert "[[reference/start-here|Start here]]" in index and "[[reference/area-unsorted|Area: Unsorted]]" in index


PERSON = ('---\ntitle: "Pat Example"\ntype: "person"\nscope: "work"\nrole: "Ops lead"\norg: "Example Co"\n'
          'emails: ["pat.example@example.com"]\nsource_ids: ["x"]\n---\n\n# Pat Example\n\n'
          'Reach at pat.example@example.com or 555-555-0142. Works on [[projects/widgets|WIDGETS]].\n')


def test_person_pages_keep_emails_but_not_phones(wiki):
    r = wiki.write("people/pat-example", PERSON, "person")
    page = wiki.read("people/pat-example")
    assert page.count("pat.example@example.com") == 2          # frontmatter and body kept
    assert "555-0142" not in page and r["scrubbed"] == {"phone": 1}


def test_other_pages_lose_emails_in_frontmatter_as_well_as_body(wiki):
    page = NOTE.format(t="Leaky", extra='owner: "pat.example@example.com"\ncc: ["someone@example.com", "ops@example.com"]\n',
                       body="Mail pat.example@example.com.")
    r = wiki.write("notes/leaky", page, "x")
    text = wiki.read("notes/leaky")
    assert "pat.example@" not in text and "someone@example.com" not in text and "ops@example.com" in text
    assert r["scrubbed"]["email"] == 3


def test_people_relation_and_people_hub(wiki):
    wiki.write("people/pat-example", PERSON, "person")
    wiki.write("notes/meeting", NOTE.format(t="Ops sync", extra='people: ["people/pat-example"]\n', body="Discussed pickups."), "x")
    assert {"path": "notes/meeting", "title": "Ops sync", "via": "people"} in wiki.links("people/pat-example")["backlinks"]
    hub = wiki.read("reference/people")
    assert "[[people/pat-example|Pat Example]]: Ops lead, Example Co (1 linked pages)" in hub
    assert "[[reference/people|People]]: 1 person pages" in wiki.read("reference/start-here")
    with pytest.raises(WikiError):
        wiki.write("reference/people", NOTE.format(t="x", extra="", body="hand edit"), "x")


def test_context_finds_misspelt_names(wiki):
    wiki.write("people/jon-example", PERSON.replace("Pat Example", "Jon Example"), "person")
    c = wiki.context("Jonn Example")
    assert c["results"][0]["path"] == "people/jon-example"
    assert wiki.context("who is Jonn")["results"][0]["path"] == "people/jon-example"
    assert "jonn~jon" in wiki.context("Jonn")["results"][0]["matched"]
    assert wiki.close_titles("zzqqxx") == []


def test_context_puts_the_named_page_ahead_of_pages_that_mention_it(wiki):
    wiki.write("people/jon-example", PERSON.replace("Pat Example", "Jon Example"), "person")
    wiki.write("notes/voice-rule", NOTE.format(t="Voice rule", extra="", body="Who is Jonn? Jonn is how Jon gets transcribed. Who what is."), "x")
    assert wiki.context("who is Jonn")["results"][0]["path"] == "people/jon-example"
    assert wiki.search("who is widgets") == wiki.search("widgets")      # question words change nothing


def test_start_here_lists_topic_overviews(wiki):
    wiki.write("concepts/release-flow", NOTE.format(t="Release flow (overview)", extra="", body="Synthesis of the release notes."), "x")
    start = wiki.read("reference/start-here")
    assert "## Topic overviews" in start and "[[concepts/release-flow|Release flow (overview)]]" in start
    assert ("orphan", "concepts/release-flow.md") not in {(i["kind"], i.get("page")) for i in wiki.lint()["examples"]}
