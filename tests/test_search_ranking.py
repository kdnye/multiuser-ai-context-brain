"""Search ranking (BM25 over whole words + key-field boost), 2026-10-29: measured with tools.eval_recall
(hit@3 0.68 -> 0.89 on 28 real questions). These pin the failure modes the baseline showed."""

from __future__ import annotations

from mcp_server.store import CachedStore
from mcp_server.wiki_store import Wiki
from tests.test_wiki_relations import NOTE, Mem


def note(title, body, extra=""):
    return NOTE.format(t=title, extra=extra, body=body)


def test_a_huge_log_page_does_not_outrank_the_focused_page():
    w = Wiki(CachedStore(Mem()), today=lambda: "2026-10-29")
    filler = " ".join(f"vendor email {i} about printers lisbon tickets widgets accounting" for i in range(2000))   # ~150 KB, under the 200 KB cap
    w.write("projects/vendor-log", note("Vendor email watch log", filler), "seed")
    w.write("projects/office-phones", note("Hosted voice setup", "New desk phones and numbers for the Lisbon office."), "seed")
    top = [r["path"] for r in w.search("desk phones Lisbon", 3)]
    assert top[0] == "projects/office-phones"


def test_role_and_nickname_fields_count_like_the_title():
    w = Wiki(CachedStore(Mem()), today=lambda: "2026-10-29")
    w.write("people/pat-chief", note("Pat Chief", "Runs the company.", 'role: "CEO"\nnickname: "Patch"\n').replace('"note"', '"person"'), "seed")
    w.write("notes/meeting", note("Board meeting notes", "The CEO asked about the CEO dashboard and CEO goals. " * 5), "seed")
    assert w.search("CEO", 2)[0]["path"] == "people/pat-chief"
    assert w.search("Patch", 1)[0]["path"] == "people/pat-chief"


def test_terms_match_whole_words_not_substrings():
    w = Wiki(CachedStore(Mem()), today=lambda: "2026-10-29")
    w.write("notes/process", note("Process notes", "A process that proceeds; nothing about leadership."), "seed")
    assert w.search("ceo", 5) == []                      # "ceo" is not inside "process" or "proceeds" any more
    w.write("notes/phone", note("Phone list", "One phone per desk."), "seed")
    assert w.search("phones", 1)[0]["path"] == "notes/phone"   # light plural folding
