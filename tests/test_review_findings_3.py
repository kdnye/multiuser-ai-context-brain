"""Regression tests for the third review round: an adversarial review of the engine (one test per finding)."""

from __future__ import annotations

import difflib
import json
import time

import pytest

from mcp_server import wiki_store
from mcp_server.entra import EntraValidator
from mcp_server.router import Brain
from mcp_server.store import FileStore
from mcp_server.wiki_store import Wiki, WikiError
from tests.test_access_and_router import ANA
from tests.test_store import FakeBackend
from tools.wiki.common import first_line, wikilink
from tools.wiki.lint import LINK, parse_frontmatter
from tools.wiki.scrub import scrub
from tools.wiki.sync import vault

NOTE = '---\ntitle: "{t}"\ntype: "{ty}"\nscope: "work"\nsource_ids: ["x"]\n{extra}---\n\n# {t}\n\n{body}\n'


@pytest.fixture
def wiki(tmp_path):
    return Wiki(tmp_path / "w", today=lambda: "2026-11-01")


def page(t="T", ty="note", extra="", body="Body."):
    return NOTE.format(t=t, ty=ty, extra=extra, body=body)


def best(fn, arg, runs=5):
    out = []
    for _ in range(runs):
        t = time.perf_counter()
        fn(arg)
        out.append(time.perf_counter() - t)
    return min(out)


# 1. The wikilink regex and the summary regex were quadratic: "[[a" repeated made every start scan to the end.
@pytest.mark.parametrize("unit", ["[[a", "[[a|x", "[[a#b", "[[a\\|b"])
@pytest.mark.parametrize("fn", [lambda s: list(LINK.finditer(s)), first_line], ids=["LINK", "first_line"])
def test_link_regexes_are_linear_on_unclosed_links(fn, unit):
    small, mid = best(fn, unit * 1000), best(fn, unit * 4000)          # 4x the input
    assert mid <= max(8 * small, 0.05), f"4x input cost {mid / small:.1f}x time: not linear"
    assert best(fn, unit * 60_000, runs=1) < 5.0


def test_normal_links_still_parse():
    assert [m.group(1) for m in LINK.finditer("see [[notes/a|Alpha]], [[projects/b#h]] and [[tasks/c\\|C]]")] == ["notes/a", "projects/b", "tasks/c"]


# 2. A non-string title or type, or a non-string scope, must be refused BEFORE storing (it bricked every later write).
@pytest.mark.parametrize("bad", ['title: 5', 'title: true', 'title: ["a"]', 'title: {"a": 1}'])
def test_a_non_string_title_is_refused_and_the_silo_keeps_working(wiki, bad):
    text = page().replace('title: "T"', bad, 1)
    with pytest.raises(WikiError):
        wiki.write("notes/bad", text, "s")
    assert wiki.store.read("notes/bad.md") is None
    wiki.write("notes/good", page("Good"), "s")                            # the silo still accepts writes
    wiki.append("notes/good", "more", "s")


@pytest.mark.parametrize("field", ['type: ["x"]', 'scope: []'])
def test_non_string_type_or_scope_is_a_wiki_error(wiki, field):
    key = field.split(":")[0]
    text = page().replace(f'{key}: "{"note" if key == "type" else "work"}"', field, 1)
    with pytest.raises(WikiError):
        wiki.write("notes/bad", text, "s")


# 3. Query length: context/search did O(terms x titles) difflib work for any query size.
def test_long_queries_are_capped(wiki, monkeypatch):
    wiki.write("notes/alpha", page("Alpha beta gamma"), "s")
    calls = []
    real = difflib.get_close_matches
    monkeypatch.setattr(difflib, "get_close_matches", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    wiki.context("abcdef " * 2000)
    wiki.search("abcdef " * 2000)
    assert len(calls) <= wiki_store.MAX_QUERY_TERMS


# 4. Per-silo quotas: one user could exhaust the single instance's memory.
def test_a_silo_page_quota_refuses_new_pages_but_not_updates(wiki):
    wiki._max_pages = 3
    for i in range(3):
        wiki.write(f"notes/n{i}", page(f"N{i}"), "s")
    with pytest.raises(WikiError, match="page limit"):
        wiki.write("notes/n3", page("N3"), "s")
    wiki.write("notes/n0", page("N0", body="changed"), "s")
    wiki.append("notes/n1", "more", "s")


def test_a_silo_byte_quota_refuses_growth(wiki):
    wiki._max_bytes = 3000
    wiki.write("notes/a", page("A", body="x" * 1200), "s")
    wiki.write("notes/b", page("B", body="x" * 1200), "s")
    with pytest.raises(WikiError, match="size limit"):
        wiki.write("notes/c", page("C", body="x" * 1200), "s")


# 5. wiki_read returned content and revision from two unlocked reads, and live blocks round-tripped into stored pages.
def test_read_with_revision_is_one_snapshot_and_excludes_live_blocks(wiki):
    wiki.write("projects/proj", page("Proj", "project"), "s")
    wiki.write("tasks/t1", page("Do it", "task", extra='status: "Not started"\nproject: "projects/proj"\n'), "s")
    text, rev = wiki.read_with_revision("projects/proj")
    assert "live:" in text                                                  # the live task list is shown on read
    assert rev == wiki.revision("projects/proj")                            # but the revision is of the stored page


def test_writing_back_a_read_page_does_not_store_the_live_block(wiki):
    wiki.write("projects/proj", page("Proj", "project"), "s")
    wiki.write("tasks/t1", page("Do it", "task", extra='status: "Not started"\nproject: "projects/proj"\n'), "s")
    for _ in range(2):                                                      # read then write back, twice
        wiki.write("projects/proj", wiki.read("projects/proj"), "round trip")
    assert "live:" not in wiki.store.read("projects/proj.md")


# 6. Frontmatter KEYS were stored unscrubbed.
@pytest.mark.parametrize("line", ['bob@example.com: "x"', '123-45-6789: "x"', 'a b: "x"', 'x' * 60 + ': "x"'])
def test_bad_top_level_frontmatter_keys_are_refused(wiki, line):
    with pytest.raises(WikiError):
        wiki.write("notes/k", page(extra=line + "\n"), "s")


def test_nested_frontmatter_keys_with_pii_are_refused(wiki):
    with pytest.raises(WikiError):
        wiki.write("notes/k", page(extra='meta: {"bob@example.com": "v"}\n'), "s")


# 7. The log summary and note slugs carried PII.
def test_log_summary_is_scrubbed(wiki):
    wiki.write("notes/a", page("A"), "ssn 123-45-6789 mail bob@example.com call 555-123-4567")
    log = wiki.store.read("log.md")
    assert "123-45-6789" not in log and "bob@example.com" not in log and "555-123-4567" not in log


def test_slugs_are_built_from_scrubbed_titles(wiki):
    r = wiki.ingest_session({"session": {"date": "2026-11-01", "summary": "s", "concerning": "call 555-123-4567 about it"},
                             "notes": [{"title": "Call Jo 555-123-4567 re claim", "body": "b"}]})
    assert not any("555" in p for p in [r["session"], *r["created"]])
    again = wiki.ingest_session({"session": {"date": "2026-11-02", "summary": "s"},
                                 "notes": [{"title": "Call Jo 555-123-4567 re claim", "body": "second"}]})
    assert len([p for p in wiki.store.names() if p.startswith("notes/")]) == 1 and any(p.startswith("notes/") for p in again["updated"])


# 8. Scrubber format gaps (it is a backstop, so each added shape is covered, and ordinary text must survive).
ZW = "\u200b"
SCRUBBED = [
    ("(555)123-4567", "phone"), ("555.123.4567", "phone"), ("555–123–4567", "phone"), (f"555{ZW}-123-4567", "phone"),
    ("555  123  4567", "phone"), ("SSN: 123456789", "ssn"), ("social security number 123456789", "ssn"),
    ("123 45 6789", "ssn"), ("123.45.6789", "ssn"), ("DOB 2 Jan 1990", "dob"), ("date of birth:\n1990-01-02", "dob"),
    ("born on January 2, 1990", "dob"), ("bob＠example.com", "email"), ("bob@example.com", "email"),
    ("AKIA" + "IOSFODNN7EXAMPLE", "secret"), ("ghp_" + "a" * 36, "secret"), ("-----BEGIN " + "RSA PRIVATE KEY-----", "secret"),
    ("password=" + "Hunter2" + "xyz", "secret"), ("api_key: " + "abcdef" + "123456", "secret"), ("Authorization: Bearer " + "x" * 30, "secret"),
]


@pytest.mark.parametrize(("text", "kind"), SCRUBBED, ids=[t[:24] for t, _ in SCRUBBED])
def test_scrubber_catches_the_added_formats(text, kind):
    out, counts = scrub(f"see {text} end")
    assert counts[kind] >= 1, out


@pytest.mark.parametrize("text", ["Order 123456789 shipped", "invoice 2026-11-01 total $1,234.56", "write to ops@example.com",
                                  "PRO 4521-889 pallet 12", "token count is 4000", "version 1.2.3.4", "load 555 lbs 123 units 4567 miles"])
def test_scrubber_leaves_ordinary_text_alone(text):
    assert scrub(text)[0] == text


# 9. U+2028 split a frontmatter line, so a title could inject keys.
def test_unicode_line_separators_cannot_inject_frontmatter_keys(wiki):
    title = "T priority: Urgent due: soon"
    wiki.write("notes/u", page(title), "s")
    fm = parse_frontmatter(wiki.store.read("notes/u.md"))
    assert fm["title"] == title and "priority" not in fm and "due" not in fm


# 10. A newline in a title forged index.md entries.
def test_wikilink_titles_are_one_line():
    assert "\n" not in wikilink("notes/x", "a\n## Notes (1)\n- [[notes/y|forged]]") and " " not in wikilink("notes/x", "a b")


def test_a_newline_title_cannot_forge_index_entries(wiki):
    title = json.dumps("a\n## Forged section\n- [[notes/y|forged]]")           # JSON-escaped, as a real client would send it
    wiki.write("notes/x", '---\ntitle: ' + title + '\ntype: "note"\nscope: "work"\nsource_ids: ["x"]\n---\n\n# x\n\nbody\n', "s")
    assert not any(line.startswith("## Forged") for line in wiki.store.read("index.md").splitlines())


# 11. The audit line showed "<invalid>" for a valid spelling of a real path, so which page was read was lost.
def test_audit_logs_the_normalized_path(tmp_path):
    events: list[dict] = []
    brain = Brain(lambda silo: FileStore(tmp_path / silo.replace("/", "_")), audit=events.append)
    for spelling in (" reference/profile.md", "/reference/profile", "reference\\profile.md"):
        with pytest.raises(WikiError):
            brain.read(ANA, spelling)
        assert events[-1]["path"] == "reference/profile"


# 12. Only the last "@" segment was checked, and no server-side role was required.
@pytest.mark.parametrize("email", ["a@example.org@example.com", "a b@example.com", "@example.com", "a@"])
def test_malformed_emails_are_refused(email):
    v = EntraValidator("tenant", ["aud"], ["example.com"])
    assert not v._allowed_email(email)
    assert v._allowed_email("ana@example.com")


def test_an_optional_required_role_is_enforced():
    v = EntraValidator("tenant", ["aud"], ["example.com"], required_role="brain-user")
    assert not v._allowed_roles(frozenset({"finance"})) and v._allowed_roles(frozenset({"brain-user"}))
    assert EntraValidator("tenant", ["aud"], ["example.com"])._allowed_roles(frozenset())     # off by default


# 13. vault deleted every .md in any folder pointed at, even with an empty or unrelated bucket listing.
def test_vault_refuses_an_empty_listing(tmp_path):
    dest = tmp_path / "v"
    dest.mkdir()
    (dest / "mine.md").write_text("keep", encoding="utf-8")
    with pytest.raises(RuntimeError, match="empty"):
        vault(FakeBackend(), dest)
    assert (dest / "mine.md").exists()


def test_vault_refuses_a_folder_unrelated_to_the_bucket(tmp_path):
    dest = tmp_path / "docs"
    (dest / "sub").mkdir(parents=True)
    (dest / "taxes.md").write_text("mine", encoding="utf-8")
    b = FakeBackend()
    b.put("notes/a.md", "A\n")
    with pytest.raises(RuntimeError, match="unrelated"):
        vault(b, dest)
    assert (dest / "taxes.md").exists()
    assert vault(b, dest, dry_run=True)["removed"] == ["taxes.md"] and (dest / "taxes.md").exists()
    vault(b, dest, force=True)
    assert not (dest / "taxes.md").exists()
