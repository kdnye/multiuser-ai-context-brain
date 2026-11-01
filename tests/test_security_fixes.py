"""Hardening pass: bounded regexes, size caps, source-id validation, oid-keyed silos, input clamps, error hygiene,
cache eviction, and keeping blocking work off the event loop."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_server import dates, entra
from mcp_server.access import Identity, can_read, readable, resolve, silo_pattern
from mcp_server.router import Brain
from mcp_server.wiki_store import MAX_BYTES, PATH, Wiki, WikiError
from tests.test_access_and_router import Mem
from tools.wiki.scrub import scrub

BIG = 200_000
FAST = 0.5


def timed(fn, *a):
    t = time.perf_counter()
    out = fn(*a)
    return out, time.perf_counter() - t


def note(title="T", sources='["x"]', extra="", body="Some body text."):
    return f'---\ntitle: "{title}"\ntype: "note"\nscope: "work"\nsource_ids: {sources}\n{extra}---\n\n# {title}\n\n{body}\n'


@pytest.fixture
def wiki(tmp_path):
    return Wiki(tmp_path, today=lambda: "2026-10-28")


# --- 1. scrub: no quadratic regex ---------------------------------------------------------------------------------
# Each builder makes an adversarial string of about n characters. The test checks how the cost SCALES, not how fast
# the machine is: a fixed wall-clock limit flaked on a busy or slow box (the same input took 0.25s one run and 0.9s the
# next), and it cannot tell a slow machine from a quadratic regex. 4x the input should cost about 4x the time; a
# quadratic regex costs about 16x. We take the best of a few runs per size (noise only ever adds time), allow a wide
# margin, and keep a generous absolute ceiling as a backstop.
ADVERSARIAL = {
    "a*": lambda n: "a" * n,
    "a.*": lambda n: "a." * (n // 2),
    "1*": lambda n: "1" * n,
    "1 *": lambda n: "1 " * (n // 2),
    "1-*": lambda n: "1-" * (n // 2),
    "a@*": lambda n: "a@" * (n // 2),
    "account*": lambda n: "account " * (n // 8),
    "dob*": lambda n: "dob " * (n // 4),
    "at-then-dots": lambda n: "a@" + "b." * (n // 2),
    "a*@b.com": lambda n: "a" * n + "@b.com",
    "(*": lambda n: "(" * n,
    "birthday+spaces": lambda n: "birthday" + " " * n,
    "account a*": lambda n: "account a" * (n // 10),
}
SCALE_RUNS, SCALE_MARGIN, SCALE_CEILING = 5, 8, 5.0


def best_time(fn, arg, runs=SCALE_RUNS):
    return min(timed(fn, arg)[1] for _ in range(runs))


@pytest.mark.parametrize("build", ADVERSARIAL.values(), ids=ADVERSARIAL.keys())
def test_scrub_is_linear_on_adversarial_input(build):
    # Compare 1/8 and 1/2 of BIG (4x apart), where the scrubber is cleanly linear (about 4x time); a quadratic
    # regex costs about 16x there, so the 8x margin separates them with room for noise. Past ~100k characters
    # memory pressure adds a mild extra cost that is not a complexity problem, so the full BIG input is only
    # held to the absolute ceiling.
    small, mid = best_time(scrub, build(BIG // 8)), best_time(scrub, build(BIG // 2))
    assert mid <= max(SCALE_MARGIN * small, 0.05), f"4x input cost {mid / small:.1f}x time: not linear"
    assert timed(scrub, build(BIG))[1] < SCALE_CEILING


def test_email_scrub_positive_negative_and_near_miss():
    assert scrub("mail jane.doe@example.com now")[0] == "mail {email} now"
    assert scrub("ask accounting@example.com")[0] == "ask accounting@example.com"        # role mailbox kept
    assert scrub("not an email: a@b")[0] == "not an email: a@b"                           # no TLD
    assert scrub("x" * 64 + "@example.com")[0] == "{email}"                               # 64-char local part is the limit
    assert scrub("x" * 65 + "@example.com")[0] == "x{email}"                              # longer: the last 64 are still removed
    assert scrub("a@" + "d" * 63 + ".com")[0] == "{email}"
    assert "{email}" in scrub("a@sub.mail.example.net")[0]


# --- 2. source dates ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sid,want", [("claude-session:2026-10-28", "2026-10-28"), ("private:personal-directory:2026-10-27", "2026-10-27"),
                                      ("raw/claude-memory/2026-10-27/profile.md", "2026-10-27"), ("import:handbook:2026-10-27", "2026-10-27"),
                                      ("x:notadate", None), ("x", None), ("2026-10-28", None), ("claude-session:2026-10-28x", None),
                                      ("claude-session:2026-10-288", None), ("Claude:2026-10-28", None), ("a b:2026-10-28", None),
                                      ("a:2026-10-28/" + "z" * 300, None), ("", None)])
def test_source_date(sid, want):
    assert dates.source_date(sid) == want


@pytest.mark.parametrize("sid", ["a" * 90_000, "a-" * 45_000 + "!", "a/" * 90_000, "a:" * 90_000 + "2026-10-28x", "a" + "-" * 90_000 + ":"], ids=lambda v: f"{v[:6]!r}-{len(v)}")
def test_source_date_is_fast_on_long_ids(sid):
    out, secs = timed(dates.page_date, {"source_ids": [sid]})
    assert out is None and secs < FAST


def test_page_date_uses_the_first_dated_source():
    assert dates.page_date({"source_ids": ["x", "claude-session:2026-10-28", "claude-session:2026-01-01"]}) == "2026-10-28"


# --- 3. events: linear on long lines, normal behavior intact ---------------------------------------------------
@pytest.mark.parametrize("line", ["## " + " " * BIG + "(", "## " + "a " * (BIG // 2), "- 2026-10-28: " + "x" * BIG,
                                  "## Update 2026-10-28 " + " " * BIG, "## " + "\t " * (BIG // 2) + "x (2026-1"], ids=lambda v: f"{v[:6]!r}-{len(v)}")
def test_events_linear_on_long_lines(line):
    out, secs = timed(dates.events, line + "\n" + line)
    assert out == [] and secs < FAST


def test_events_normal_lines_still_parse():
    body = "## Heading (2026-10-28)\n\n## Update 2026-08-01\n\n## History\n\n- 2026-10-27: status: done\n- 2026-10-26: note\n"
    got = dates.events(body)
    assert ("2026-10-28", "Heading", "changed") in got and ("2026-08-01", "Update", "changed") in got
    assert ("2026-10-27", "status: done", "changed") in got
    assert dates.events("- 2026-10-27: elsewhere\n") == [("2026-10-27", "elsewhere", "dated")]
    assert dates.events("- 2026-10-27: " + "x" * 250)       # under the cap still parses
    assert dates.events("- 2026-10-27: " + "x" * 400) == []   # over it is prose


# --- 4. MAX_BYTES on every stored page -----------------------------------------------------------------------------
def test_ingest_session_cannot_store_an_oversized_page(wiki):
    with pytest.raises(WikiError, match="exceed"):
        wiki.ingest_session({"session": {"summary": "s" * 300_000, "date": "2026-10-28"}})
    assert not [n for n in wiki.store.names() if n.startswith("sessions/")]


def test_ingest_oversized_note_is_refused(wiki):
    with pytest.raises(WikiError, match="exceed"):
        wiki.ingest_session({"session": {"summary": "ok", "date": "2026-10-28"}, "notes": [{"title": "Big", "body": "n" * 300_000}]})
    assert "notes/big.md" not in wiki.store.names()


def test_page_just_under_the_cap_is_stored(wiki):
    assert wiki.write("notes/fits", note(body="y" * (MAX_BYTES - 1000)), "ok")["action"] == "created"


# --- 5. source ids and nested frontmatter --------------------------------------------------------------------------
@pytest.mark.parametrize("sid", ["seed:pilot-2026-10", "mcp_server/access.py", "repo/README.md@abc123", "claude-session:2026-10-28", "generated", "x"])
def test_known_source_id_formats_are_accepted(wiki, sid):
    assert wiki.write("notes/a", note(sources=f'["{sid}"]'), "ok")["action"] in ("created", "updated")


@pytest.mark.parametrize("sid", ["a b", "x" * 201, "", "a;b", "a\\u00e9", "$(x)", "{email}"])
def test_bad_source_ids_are_refused_by_name(wiki, sid):
    with pytest.raises(WikiError, match="bad source id"):
        wiki.write("notes/a", note(sources=f'["{sid}"]'), "ok")
    assert "notes/a.md" not in wiki.store.names()


def test_source_ids_must_be_strings(wiki):
    with pytest.raises(WikiError, match="bad source id"):
        wiki.write("notes/a", note(sources='[1, "x"]'), "ok")


def test_nested_frontmatter_is_scrubbed(wiki):
    extra = 'meta: {"contact": "call 555-123-4567", "who": [{"mail": "jane.doe@example.com"}]}\n'
    r = wiki.write("notes/nested", note(extra=extra), "ok")
    text = wiki.read("notes/nested")
    assert "555-123-4567" not in text and "jane.doe@example.com" not in text
    assert r["scrubbed"].get("phone") == 1 and r["scrubbed"].get("email") == 1


def test_too_deeply_nested_frontmatter_is_refused(wiki):
    deep = "1"
    for _ in range(10):
        deep = '{"a": ' + deep + "}"
    with pytest.raises(WikiError, match="nested"):
        wiki.write("notes/deep", note(extra=f"meta: {deep}\n"), "ok")


# --- 6. silos are keyed by oid -------------------------------------------------------------------------------------
G = "b0000000-0000-4000-8000-0000000000{:02x}"
COLLIDING = ["john.smith@example.com", "john_smith@example.com", "john-smith@example.com",
             "John.Smith@example.org"]


def test_colliding_emails_with_different_oids_get_four_silos_none_readable_by_another():
    people = [Identity(G.format(i), e) for i, e in enumerate(COLLIDING)]
    assert len({p.home for p in people}) == 4
    for me in people:
        assert all(not can_read(o, me.home) for o in people if o is not me)


def test_same_oid_with_a_new_email_keeps_the_silo_and_a_reused_address_does_not_inherit_it():
    old = Identity(G.format(1), "john.smith@example.com")
    renamed = Identity(G.format(1), "j.smith@example.com")
    reused = Identity(G.format(2), "john.smith@example.com")
    assert old.home == renamed.home and old.home != reused.home
    assert resolve(renamed, "me") == old.home and not can_read(reused, old.home)


def test_oid_must_be_a_guid_and_is_lowercased():
    for bad in ["oid-1", "", "x", G.format(1) + "\n", " " + G.format(1), G.format(1)[:-1], None, 5, "../../shared"]:
        with pytest.raises(ValueError):
            Identity(bad, "a@example.com")
    assert Identity(G.format(1).upper(), "a@x.example").home == "users/" + G.format(1)


def test_silo_names_are_oid_only_and_reject_trailing_newlines():
    ok = "users/" + G.format(3)
    assert silo_pattern().fullmatch(ok) and not silo_pattern().fullmatch("users/ana-ops") and not silo_pattern().fullmatch("users/" + G.format(3).upper())
    me = Identity(G.format(3), "a@example.com")
    for s in ("shared\n", "shared\r\n", "finance\n", ok + "\n", ok + "\r\n", "hr\n", "users/ana-ops"):
        with pytest.raises(PermissionError):
            resolve(me, s)
    assert resolve(me, "shared") == "shared" and readable(me)[0] == ok


def test_entra_refuses_a_token_whose_oid_is_not_a_guid():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = entra.EntraValidator("t1", ["aud"], ["example.com"], key_for=lambda t: key.public_key())

    def tok(oid):
        now = int(time.time())
        return jwt.encode({"iss": v.issuer, "aud": "aud", "tid": "t1", "oid": oid, "preferred_username": "a@example.com",
                           "nbf": now - 5, "exp": now + 300}, key, algorithm="RS256")
    assert v.identity(tok("not-a-guid")) is None
    assert v.identity(tok(G.format(9).upper())).home == "users/" + G.format(9)


def test_dev_identity_has_a_stable_guid_oid(monkeypatch):
    from mcp_server import server
    monkeypatch.setenv("BRAIN_DEV_EMAIL", "Dev@example.com")
    a, b = server._dev_identity(), server._dev_identity()
    assert a == b and silo_pattern().fullmatch(a.home)


# --- 7. trailing newline in page paths -----------------------------------------------------------------------------
@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\r", "\n\n"])
def test_page_paths_reject_trailing_newlines(wiki, suffix):
    assert not PATH.fullmatch("notes/evil" + suffix)
    for call in (lambda p: wiki.read(p), lambda p: wiki.write(p, note(), "x"), lambda p: wiki.append(p, "t", "s"), lambda p: wiki.links(p)):
        with pytest.raises(WikiError):
            call("notes/evil" + suffix)
    assert "notes/evil.md" not in wiki.store.names()
    assert PATH.fullmatch("notes/good-1")


# --- 8. limits are clamped -----------------------------------------------------------------------------------------
class Spy:
    """A Wiki stand-in that records the limits it was called with."""

    def __init__(self):
        self.calls = {}

    def context(self, topic, limit):
        self.calls["context"] = limit
        return {"results": []}

    def search_view(self, query, limit):
        self.calls["search"] = limit
        return {"results": []}

    def list_view(self, section, scope, limit, *rest):
        self.calls["list"] = limit
        return {"pages": []}

    def timeline(self, date_from, date_to, scope, per_day, section, compact=False):
        self.calls["timeline"] = per_day
        return {"days": []}


def test_limits_are_clamped():
    spy = Spy()
    me = Identity(G.format(1), "a@example.com")
    b = Brain(lambda silo: Mem(), today=lambda: "2026-10-28")
    b._wikis = {"shared": spy, me.home: spy}
    b.context(me, "topic", 10**9)
    b.search(me, "topic", 10**9)
    b.list(me, limit=10**9)
    b.timeline(me, "2026-10-01", "2026-10-02", per_day=10**9)
    assert spy.calls == {"context": 20, "search": 50, "list": 500, "timeline": 100}
    b.context(me, "t", -5)
    b.list(me, limit=0)
    assert spy.calls["context"] == 1 and spy.calls["list"] == 1
    b.context(me, "t", 7)
    b.search(me, "t", 30)
    assert spy.calls["context"] == 7 and spy.calls["search"] == 30        # in range: untouched


def test_wiki_timeline_caps_per_day_at_100(wiki):
    wiki.write("notes/a", note(extra='created: "2026-10-20"\n'), "ok")
    assert wiki.timeline("2026-10-01", "2026-10-30", per_day=10**9)["days"]


# --- 9. errors: no leaks, no half-written pages --------------------------------------------------------------------
def test_malformed_link_target_warns_and_the_write_completes(wiki):
    r = wiki.write("notes/linker", note(body="See [[../../bob/notes/x]] and [[notes/ok-missing]]."), "ok")
    assert any("../../bob/notes/x" in w for w in r["warnings"])
    assert "notes/linker" in wiki.store.read("index.md")
    assert "notes/linker" in wiki.store.read("log.md")
    assert "../../bob/notes/x" in wiki.links("notes/linker")["missing_targets"]


@pytest.fixture
def server_mod(monkeypatch):
    from mcp_server import server
    monkeypatch.setenv("BRAIN_DEV_EMAIL", "dev@example.com")
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    return server


def test_unexpected_exceptions_return_a_generic_error_and_log_only_the_type(server_mod, caplog):
    def boom(who):
        raise ValueError("secret page text and /internal/path")

    caplog.set_level(logging.INFO)
    out = asyncio.run(server_mod._run("wiki_read", None, boom))
    assert out == {"error": "internal error"}
    logged = " ".join(r.getMessage() + str(getattr(r, "fields", "")) for r in caplog.records)
    assert "ValueError" in logged and "wiki_read" in logged and "secret" not in logged and "/internal" not in logged


def test_wiki_and_permission_errors_still_reach_the_client(server_mod):
    def deny(who):
        raise PermissionError("no access to silo 'finance'")

    def bad(who):
        raise WikiError("bad page path")
    assert asyncio.run(server_mod._run("t", None, deny)) == {"error": "no access to silo 'finance'"}
    assert asyncio.run(server_mod._run("t", None, bad)) == {"error": "bad page path"}


# --- 10. entra cache and JWKS off the loop ----------------------------------------------------------------------------
def _validator(key, key_for=None):
    return entra.EntraValidator("t1", ["aud"], ["example.com"], key_for=key_for or (lambda t: key.public_key()))


def _tok(v, key, n, exp_in=300):
    now = int(time.time())
    return jwt.encode({"iss": v.issuer, "aud": "aud", "tid": "t1", "oid": G.format(n % 256), "preferred_username": f"u{n}@example.com",
                       "jti": str(n), "nbf": now - 5, "exp": now + exp_in}, key, algorithm="RS256")


def test_token_cache_drops_expired_entries_and_is_bounded(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = _validator(key)
    monkeypatch.setattr(entra, "CACHE_MAX", 5)
    for n in range(12):
        assert v.identity(_tok(v, key, n))
        assert len(v._cache) <= 5
    v._cache["stale"] = (time.time() - 10, Identity(G.format(1), "a@example.com"))
    v.identity(_tok(v, key, 99))
    assert "stale" not in v._cache


def test_slow_jwks_fetch_does_not_block_the_event_loop():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def slow(token):
        time.sleep(0.4)
        return key.public_key()
    v = _validator(key, slow)
    tok = _tok(v, key, 1)

    async def main():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.01)
        t = asyncio.create_task(ticker())
        ident = await v.identity_async(tok)
        stop = True
        await t
        return ident, ticks
    ident, ticks = asyncio.run(main())
    assert ident is not None and ticks > 10          # the loop kept running during the 0.4 s fetch


# --- 11. tool bodies run in a worker thread -----------------------------------------------------------------------
def test_brain_calls_run_off_the_event_loop_thread(server_mod, monkeypatch):
    seen = {}

    class FakeBrain:
        def read(self, who, path, silo):
            seen["thread"] = threading.get_ident()
            seen["who"] = who
            return {"ok": True}
    monkeypatch.setattr(server_mod, "brain", lambda: FakeBrain())

    async def main():
        seen["loop"] = threading.get_ident()
        return await server_mod.wiki_read("notes/x", None)
    out = asyncio.run(main())
    assert out == {"ok": True} and seen["thread"] != seen["loop"] and seen["who"].email == "dev@example.com"
