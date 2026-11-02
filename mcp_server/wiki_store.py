"""The wiki as a Python object: everything the MCP tools do, with no MCP dependency (easy to test).

Safety rules enforced here, not left to the caller:
- page paths are `section/slug` only, from a fixed list of sections; nothing outside the wiki;
- every write passes the scrubber (STANDARDS rule 3) and must carry the required frontmatter;
- every write rebuilds index.md and appends to log.md, the two files the LLM navigates by;
- writes are serialized by a lock (the hosted service is one long-lived process).

Pages live in a `Store` (mcp_server/store.py): a folder on disk, or Cloud Storage behind a cache.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import math
import os
import re
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from mcp_server import dates, employee, records
from mcp_server.store import FileStore, Store
from tools.wiki.common import first_line, frontmatter, wikilink
from tools.wiki.lint import (
    FRONTMATTER,
    LINK,
    REQUIRED,
    SCOPES,
    lint_pages,
    parse_frontmatter,
    relation_targets,
)
from tools.wiki.scrub import scrub
from tools.wiki.slug import slugify

SECTIONS = ("projects", "notes", "sessions", "repos", "reference", "pages", "concepts", "answers", "people", "tasks", "future-needs", *employee.SECTIONS)
# Always used with fullmatch: `$` would also accept a trailing newline, which on GCS injects lines into index.md.
PATH = re.compile(r"(?:" + "|".join(SECTIONS) + r")/[a-z0-9][a-z0-9-]{0,120}")
SOURCE_ID = re.compile(r"[A-Za-z0-9:/._@-]{1,200}")
MAX_BYTES = 200_000                     # every stored page: write, append, and each page ingest_session creates
MAX_FM_DEPTH = 6
FM_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,40}")      # a frontmatter key is a plain identifier, never free text
# Query caps: close_titles runs difflib for every term against every title word, so cost grew with query length.
MAX_QUERY, MAX_QUERY_TERMS = 300, 12
# Per-silo quotas. A cached page costs about 10x its size in memory (text, lowercase copy, token counts), and the hosted
# service is one small instance, so one silo must not be able to exhaust it. Override with BRAIN_MAX_PAGES and
# BRAIN_MAX_SILO_BYTES; the defaults suit a few thousand pages on a 512 MiB instance.
DEFAULT_MAX_PAGES, DEFAULT_MAX_SILO_BYTES = 5_000, 40_000_000
START = "reference/start-here"
PEOPLE_HUB = "reference/people"
AREA_PREFIX = "reference/area-"
ACTIVE = ("in progress", "active", "next", "planning", "blocked", "on hold")
# Question words carry no topic: "who is Jonn" is a search for "jonn".
STOPWORDS = frozenset(["who", "what", "whats", "where", "when", "why", "how", "is", "are", "was", "were", "the", "a", "an", "of", "to", "for", "and", "or", "about", "do", "does", "did", "we", "you", "i", "me", "my", "know", "tell", "remember", "recall", "any", "anything", "thing", "things", "that", "this", "there", "doing", "working", "worked", "on", "around", "during", "happen", "happened"])
LOG_HEADER = '---\ntitle: "Log"\ntype: "log"\n---\n\n# Log\n\nAppend-only.\n'
# Frontmatter fields that name what a page IS; a query term found here counts like a title match (search ranking).
KEY_FIELDS = ("title", "aliases", "nickname", "role", "org", "tags")
_WORD = re.compile(r"[a-z0-9]+")


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


def _clean(text) -> str:
    """A caller-supplied name or title as it will be STORED (scrubbed, trimmed). Slugs and title matching use this, so a
    phone number in a title never reaches a page path, the index or the log, and the same title finds its own page."""
    return scrub(text if isinstance(text, str) else "")[0].strip()


_LIVE_BLOCK = re.compile(r"\n---\n\n## (?:Everything for this role|Tasks) \(live: ")


def _strip_live_blocks(content: str) -> str:
    """Cut the computed lists wiki_read appends (role portal, project and session task lists). Writing a page back as
    read stored them as static text, and every round trip added another stale copy."""
    m = _LIVE_BLOCK.search(content)
    return content if m is None else content[: m.start()].rstrip("\n") + "\n"


def _norm(token: str) -> str:
    """Light plural folding so "phones" finds "phone" (not a stemmer: only a trailing s on longer words)."""
    return token[:-1] if len(token) > 3 and token.endswith("s") and not token.endswith("ss") else token


def _tokens(text: str) -> list[str]:
    return [_norm(t) for t in _WORD.findall(text) if len(t) >= 2]


class WikiError(ValueError):
    """A request the wiki refuses; the message says why."""


class Wiki:
    def __init__(self, root: str | Path | Store, today: Callable[[], str] | None = None, personal: bool = False):
        self._base_store: Store = FileStore(root) if isinstance(root, (str, Path)) else root
        self._staging = threading.local()    # the ingesting thread's staged view; every other thread keeps the real store
        self._today = today or (lambda: dt.datetime.now(dt.UTC).date().isoformat())
        self._lock = threading.RLock()
        self._personal = personal        # a user's own silo: their person pages may keep phone numbers (never shared or role silos)
        self._meta_cache: dict[str, tuple[str, dict]] = {}
        self._deferred: list[tuple[str, str, str]] | None = None   # (kind, page, summary) while a batch defers bookkeeping
        self._max_pages = _env_int("BRAIN_MAX_PAGES", DEFAULT_MAX_PAGES)
        self._max_bytes = _env_int("BRAIN_MAX_SILO_BYTES", DEFAULT_MAX_SILO_BYTES)

    @property
    def store(self) -> Store:
        """The store this thread reads and writes: its own staging layer while it runs an ingest, else the real one.
        Staging is per thread so a concurrent reader never sees pages of an ingest that has not been committed."""
        staged = getattr(self._staging, "store", None)
        return self._base_store if staged is None else staged

    @store.setter
    def store(self, value: Store) -> None:
        self._base_store = value

    def _meta(self, name: str) -> dict:
        """Parsed view of one page (frontmatter, body, lowercased text, summary), computed once per page version.

        Scale (2026-10-27 benchmark): re-parsing every page on every search took 4 s at 10,000 pages. The cache is
        keyed on the exact text object the store returns, so a CachedStore (same object until the page is
        rewritten) parses each version once, and a FileStore (a fresh string per read) simply re-parses."""
        text = self.store.read(name) or ""
        hit = self._meta_cache.get(name)
        if hit is not None and hit[0] is text:
            return hit[1]
        fm = parse_frontmatter(text)
        body = FRONTMATTER.sub("", text, count=1)
        links = list(dict.fromkeys(m.group(1).strip() for m in LINK.finditer(body)))
        low = body.lower()
        key_text = " ".join(str(v) for k in KEY_FIELDS for v in (fm.get(k) if isinstance(fm.get(k), list) else [fm.get(k)]) if v)
        meta = {"fm": fm, "body": body, "low": low, "title_low": str(fm.get("title", "")).lower(), "summary": first_line(body),
                "links": links, "relations": relation_targets(fm),
                "date": dates.page_date(fm), "events": dates.events(body),   # computed once per page version
                "tokens": Counter(_tokens(low)), "key_tokens": set(_tokens(key_text.lower()))}
        meta["length"] = sum(meta["tokens"].values())
        self._meta_cache[name] = (text, meta)
        return meta

    # --- paths ---------------------------------------------------------------------------
    @staticmethod
    def normalize_path(page) -> str:
        """The path a request names, normalized the way every tool does (the audit log records this, not the spelling)."""
        raw = page if isinstance(page, str) else ""
        return raw.strip(" \t").replace("\\", "/").lstrip("/").removesuffix(".md")

    def _page(self, page: str) -> tuple[str, str]:
        """(page path, store name) for a request, or a WikiError."""
        p = self.normalize_path(page)
        if p not in ("index", "log") and not PATH.fullmatch(p):
            raise WikiError(f"bad page path {page!r}: use section/slug with a section in {', '.join(SECTIONS)}, lowercase letters, digits and hyphens")
        return p, f"{p}.md"

    def _all(self) -> list[tuple[str, str]]:
        """(page path, store name) for every content page, in section order."""
        names = self.store.names()
        out = []
        for section in SECTIONS:
            out += [(n[:-3], n) for n in names if n.startswith(section + "/") and "/" not in n[len(section) + 1:]]
        return out

    # --- reading -------------------------------------------------------------------------
    def read(self, page: str) -> str:
        return self.read_with_revision(page)[0]

    def read_with_revision(self, page: str) -> tuple[str, str]:
        """(text as shown, revision) from ONE read of the stored page. wiki_read used to read the page and then ask for
        its revision separately, so a write between the two returned old text with the new revision, and a following
        wiki_write(expected_revision=...) then overwrote that concurrent change. The revision is of the stored text
        (live portal and task lists added on read are not part of it)."""
        p, name = self._page(page)
        stored = self.store.read(name)
        if stored is None:
            raise WikiError(f"no such page: {p}")
        text = stored
        if p.startswith(employee.ROLES + "/"):
            text = text.rstrip("\n") + "\n" + employee.portal_block(self, p)
        page_type = parse_frontmatter(stored).get("type")
        if page_type in ("project", "session"):
            text = text.rstrip("\n") + "\n" + records.live_block(self, p, page_type)
        return text, hashlib.sha256(stored.encode("utf-8")).hexdigest()[:12]

    def index_view(self, section: str | None = None, offset: int = 0, limit: int = 300) -> str:
        """A table of contents (section names and counts), or one section's catalog, `limit` entries at a time from
        `offset`. The full index is far too big to read whole, and one section can be too (notes reaches hundreds
        of KB at a few thousand pages)."""
        text = self.store.read("index.md")
        if text is None:
            raise WikiError("the wiki has no index.md yet")
        head, *blocks = re.split(r"(?m)^## ", text)
        if section is None:
            toc = "\n".join(f"- {b.splitlines()[0]}" for b in blocks)
            return f"{head.rstrip()}\n\n## Sections\n\n{toc}\n\nCall wiki_index(section=...) for a section's catalog, or wiki_search for keywords.\n"
        for b in blocks:
            if b.lower().startswith(section.lower() + " ("):
                heading, *rest = b.splitlines()
                entries = [line for line in rest if line.startswith("- ")]
                offset, limit = max(0, offset), max(1, min(limit, 1000))
                chunk = entries[offset: offset + limit]
                out = f"## {heading}\n\n" + "\n".join(chunk) + "\n"
                if offset + limit < len(entries):
                    out += f"\n({offset + 1}-{offset + len(chunk)} of {len(entries)}; call again with offset={offset + limit} for more)\n"
                return out
        raise WikiError(f"no such section {section!r}; sections: " + ", ".join(b.splitlines()[0] for b in blocks))

    def list_pages(self, section: str | None = None, scope: str | None = None, limit: int = 200, date_from: str | None = None,
                   date_to: str | None = None, sort: str = "path", status: str | None = None, not_status: str | None = None) -> list[dict]:
        """Pages, optionally filtered by section, scope, activity date range (dates.page_date, never `updated`) and
        record status; sorted by path, activity date or `updated`."""
        limit = max(1, limit)
        try:
            date_from, date_to = dates.valid(date_from, "date_from"), dates.valid(date_to, "date_to")
        except ValueError as exc:
            raise WikiError(str(exc)) from None
        if sort not in dates.SORTS:
            raise WikiError(f"sort must be one of {', '.join(dates.SORTS)}")
        dated = date_from is not None or date_to is not None
        rows = []
        for p, name in self._all():
            if self._generated(p) or (section and not p.startswith(section + "/")):
                continue
            fm = self._meta(name)["fm"]
            if scope and fm.get("scope") != scope:
                continue
            have = str(fm.get("status") or "").casefold()
            if status and have != status.casefold() or not_status and have == not_status.casefold():
                continue
            d = self._meta(name)["date"]
            if dated and not dates.in_range(d, date_from, date_to):
                continue
            row = {"path": p, "title": fm.get("title"), "type": fm.get("type"), "scope": fm.get("scope"), "date": d, "updated": fm.get("updated")}
            if fm.get("status"):
                row["status"] = fm["status"]
            rows.append(row)
        if sort in ("date_desc", "date_asc"):
            undated = [r for r in rows if not r["date"]]
            rows = sorted((r for r in rows if r["date"]), key=lambda r: (r["date"], r["path"]), reverse=sort == "date_desc") + undated
        elif sort == "updated":
            rows.sort(key=lambda r: (str(r["updated"] or ""), r["path"]), reverse=True)
        else:
            rows.sort(key=lambda r: r["path"])
        return rows[:limit]

    def list_view(self, section: str | None = None, scope: str | None = None, limit: int = 200, date_from: str | None = None,
                  date_to: str | None = None, sort: str = "path", status: str | None = None, not_status: str | None = None) -> dict:
        """list_pages plus whether more exist, so a caller never mistakes a capped list for the whole section
        (found in the 2026-10-27 live test: notes has more than the default 200)."""
        limit = max(1, limit)
        pages = self.list_pages(section, scope, limit + 1, date_from, date_to, sort, status, not_status)
        truncated = len(pages) > limit
        out = {"pages": pages[:limit], "truncated": truncated}
        if truncated:
            out["hint"] = f"more than {limit} pages match; raise limit, narrow by section/scope/date, or use wiki_search"
        if not pages and (date_from or date_to):
            out["note"] = "No pages in that date range. " + dates.coverage_line(dates.coverage(self))
        return out

    def search_view(self, query: str, limit: int = 10) -> dict:
        """search(), but an ISO date in the query (YYYY-MM-DD) is an exact-date filter, not three loose numbers:
        a page matches when its activity date is that day, a dated event on it is that day, or its text contains the
        date. Extra words narrow the matches; if they would remove every one, the date-only matches are returned with
        a note saying so. Only a date with no match at all says "No entries" (plus the wiki's date coverage)."""
        query = (query or "")[:MAX_QUERY]
        wanted = dates.ISO.findall(query)
        if not wanted:
            return {"results": self.search(query, limit)}
        rest = dates.ISO.sub(" ", query).strip()
        terms = [t for t in re.split(r"[^a-z0-9]+", rest.lower()) if len(t) >= 2 and t not in STOPWORDS]
        day_hits = []
        for p, name in self._all():
            if self._generated(p):
                continue
            m = self._meta(name)
            d = m["date"]
            on_day = d in wanted
            ev = [what for ed, what, _ in m["events"] if ed in wanted]
            mentions = any(w in m["body"] for w in wanted)
            if not (on_day or ev or mentions):
                continue
            why = "dated that day" if on_day else ("changed that day: " + ev[0][:100]) if ev else "mentions the date"
            day_hits.append((0 if on_day else 1 if ev else 2, p, m, d, why))
        hits = [h for h in day_hits if all(t in h[2]["title_low"] or t in h[2]["low"] for t in terms)]
        out: dict = {"dates": wanted}
        date_only = bool(terms and day_hits and not hits)
        if date_only:
            hits = day_hits
            out["note"] = f"{len(day_hits)} pages on {', '.join(wanted)}; none also mention {' '.join(terms)}"
            out["date_only"] = True
        hits.sort(key=lambda x: (x[0], x[1]))
        results = []
        for _, p, m, d, why in hits[:limit]:
            needles = terms + wanted if not date_only else wanted
            first = min((m["low"].find(t) for t in needles if t in m["low"]), default=0)
            snippet = re.sub(r"\s+", " ", m["body"][max(0, first - 60): first + 140]).strip()
            results.append({"path": p, "title": m["fm"].get("title"), "type": m["fm"].get("type"), "scope": m["fm"].get("scope"),
                            "date": d, "matched": why, "snippet": snippet})
        out["results"] = results
        if not hits:
            out["note"] = f"No entries for {', '.join(wanted)}. " + dates.coverage_line(dates.coverage(self))
        elif len(hits) > limit:
            out["hint"] = f"{len(hits)} pages match; for a day-by-day view use wiki_timeline"
        return out

    def timeline(self, date_from: str, date_to: str, scope: str | None = None, per_day: int = 15, section: str | None = None,
                 compact: bool = False) -> dict:
        try:
            return dates.timeline(self, date_from, date_to, scope, max(1, min(per_day, 100)), section, compact=bool(compact))
        except ValueError as exc:
            raise WikiError(str(exc)) from None

    def log_view(self, n: int = 10, since: str | None = None, until: str | None = None) -> dict:
        """{entries, truncated}. Default: the last n entries (truncated False: it is the tail by definition). With
        since/until: the NEWEST n entries in that range, still oldest first; truncated says older ones in the range
        were left out. Empty-string since/until count as not given."""
        since, until, n = since or None, until or None, max(1, n)
        if since is None and until is None:
            return {"entries": self.log_tail(n), "truncated": False}
        try:
            since, until = dates.valid(since, "since"), dates.valid(until, "until")
        except ValueError as exc:
            raise WikiError(str(exc)) from None
        lines = [line for line in (self.store.read("log.md") or "").splitlines() if line.startswith("## [")]
        found = dates.log_range(lines, since, until)
        return {"entries": found[-n:], "truncated": len(found) > n}

    def search(self, query: str, limit: int = 10) -> list[dict]:
        query = (query or "")[:MAX_QUERY]
        terms = [t for t in re.split(r"[^a-z0-9]+", query.lower()) if len(t) >= 2 and t not in STOPWORDS] or \
                [t for t in re.split(r"[^a-z0-9]+", query.lower()) if len(t) >= 2]
        if not terms:
            raise WikiError("empty query")
        qtok = list(dict.fromkeys(_norm(t) for t in terms))
        cands = []
        for p, name in self._all():
            if self._generated(p):          # navigation hubs are reached through the map, not returned as answers
                continue
            m = self._meta(name)
            hits = [t for t in qtok if m["tokens"].get(t) or t in m["key_tokens"]]
            if hits:
                cands.append((p, name, m, hits))
        # BM25 (k1 1.2, b 0.75) over whole-word tokens, plus a boost when a term is in the page's own title, aliases,
        # nickname, role or tags (ported from the personal brain 2026-10-29, where it raised recall hit@3 0.68 -> 0.89:
        # long log pages no longer win every query and short terms no longer match inside other words).
        n = len(cands) or 1
        avg = sum(m["length"] for _, _, m, _ in cands) / n or 1
        df = Counter(t for _, _, m, hits in cands for t in set(hits))
        scored = []
        for p, name, m, hits in cands:
            score = 0.0
            for t in hits:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                tf = m["tokens"].get(t, 0)
                score += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * m["length"] / avg)) if tf else 0.0
                if t in m["key_tokens"]:
                    score += 3.0 * idf
            scored.append((len(hits) == len(qtok), round(score, 3), p, name, hits))
        scored.sort(key=lambda x: (not x[0], -x[1], x[2]))
        out = []
        for _, score, p, name, hits in scored[:limit]:  # snippets only for what is returned (most of the cost at scale)
            m = self._meta(name)
            first = min((i for i in (m["low"].find(t) for t in hits) if i >= 0), default=0)
            snippet = re.sub(r"\s+", " ", m["body"][max(0, first - 60): first + 140]).strip()
            out.append({"path": p, "title": m["fm"].get("title"), "type": m["fm"].get("type"), "scope": m["fm"].get("scope"),
                        "score": score, "matched": f"{len(hits)}/{len(terms)} terms", "snippet": snippet})
        return out

    def log_tail(self, n: int = 10) -> list[str]:
        lines = [line for line in (self.store.read("log.md") or "").splitlines() if line.startswith("## [")]
        return lines[-n:]

    def lint(self) -> dict:
        r = lint_pages({name: self.store.read(name) or "" for name in self.store.names()})
        # A page with an audience is reached through its role portals (computed on read), not by backlinks.
        reached = {name for _, name in self._all() if employee.audience(self._meta(name)["fm"])}
        reached.add(f"{START}.md")                # the entry point: nothing is meant to link to it
        issues = [i for i in r["issues"] if not (i["kind"] == "orphan" and i.get("page") in reached)]
        issues += employee.lint(self, self._today())
        return {"pages": r["pages"], "by_kind": dict(Counter(i["kind"] for i in issues)), "examples": issues[:20]}

    # --- relationships -------------------------------------------------------------------
    def _exists(self, p: str) -> bool:
        """Does page p exist? A malformed link target (the store refuses the name) counts as missing, never an error."""
        try:
            return self.store.read(f"{p}.md") is not None
        except Exception:  # noqa: BLE001 - any refusal of the name means "no such page"
            return False

    def _title(self, p: str) -> str | None:
        return self._meta(f"{p}.md")["fm"].get("title") if self._exists(p) else None

    def links(self, page: str, limit: int = 50) -> dict:
        """A page's neighbours: what it links to, what links to it, and its typed relations (project, related,
        supersedes ...) in both directions. Missing targets are reported, not hidden."""
        p, name = self._page(page)
        if self.store.read(name) is None:
            raise WikiError(f"no such page: {p}")
        me = self._meta(name)
        outgoing = [{"path": t, "title": self._title(t), "via": "link"} for t in me["links"]]
        outgoing += [{"path": t, "title": self._title(t), "via": rel} for rel, t in me["relations"]]
        incoming = []
        for q, qname in self._all():
            if q == p:
                continue
            m = self._meta(qname)
            via = [rel for rel, t in m["relations"] if t == p] + (["link"] if p in m["links"] else [])
            if via:
                incoming.append({"path": q, "title": m["fm"].get("title"), "via": ",".join(via)})
        missing = sorted({o["path"] for o in outgoing if o["title"] is None})
        return {"path": p, "title": me["fm"].get("title"), "outgoing": outgoing[:limit], "backlinks": incoming[:limit],
                "counts": {"outgoing": len(outgoing), "backlinks": len(incoming)}, "missing_targets": missing}

    def context(self, topic: str, limit: int = 5, neighbours: int = 5) -> dict:
        """One-call recall for a topic: the best-matching pages, each with a few neighbours, so an agent can see
        what the wiki already knows (and how it connects) before answering or writing."""
        topic = (topic or "")[:MAX_QUERY]
        if dates.ISO.search(topic):          # a date in the topic is an exact-date lookup, not loose terms
            r = self.search_view(topic, limit)
            out = []
            for h in r["results"]:
                n = self.links(h["path"], limit=neighbours)
                near = (n["outgoing"] + n["backlinks"])[:neighbours]
                out.append({**h, "neighbours": [{"path": x["path"], "title": x["title"], "via": x["via"]} for x in near if x["title"]]})
            res = {"topic": topic, "results": out, "dates": r["dates"], "profile": "reference/profile",
                   "hint": ("Read the pages that matter with wiki_read before answering; cite them by path." if out else
                            "The wiki has nothing on this date. Say so rather than guessing.")}
            for k in ("note", "date_only"):
                if k in r:
                    res[k] = r[k]
            return res
        try:
            found = self.search(topic, limit)
        except WikiError:
            found = []
        # Pages whose TITLE matches a query word (exactly or by close spelling, e.g. "Jonn" -> Jon) lead:
        # a page named for the thing beats a page that merely mentions it.
        titled = self.close_titles(topic, 2)
        seen = {h["path"] for h in titled}
        hits = (titled + [h for h in found if h["path"] not in seen])[:limit]
        out = []
        for h in hits:
            n = self.links(h["path"], limit=neighbours)
            near = (n["outgoing"] + n["backlinks"])[:neighbours]
            out.append({**h, "neighbours": [{"path": x["path"], "title": x["title"], "via": x["via"]} for x in near if x["title"]]})
        hint = ("Read the pages that matter with wiki_read before answering; cite them by path."
                if out else "The wiki has nothing on this. Say so rather than guessing; if the session produces knowledge, record it.")
        return {"topic": topic, "results": out, "profile": "reference/profile", "hint": hint}

    def close_titles(self, query: str, limit: int = 5) -> list[dict]:
        """Pages whose title words are close spellings of the query words (difflib ratio >= 0.75), for names and
        terms typed or heard slightly wrong. Titles only, so it stays cheap and precise."""
        terms = [t for t in re.split(r"[^a-z0-9]+", (query or "")[:MAX_QUERY].lower()) if len(t) >= 3 and t not in STOPWORDS]
        terms = list(dict.fromkeys(terms))[:MAX_QUERY_TERMS]
        if not terms:
            return []
        by_word: dict[str, set[str]] = {}
        titles: dict[str, tuple[str, dict]] = {}
        for p, name in self._all():
            if self._generated(p):
                continue
            m = self._meta(name)
            titles[p] = (name, m)
            for w in set(re.split(r"[^a-z0-9]+", m["title_low"])):
                if len(w) >= 3:
                    by_word.setdefault(w, set()).add(p)
        score: dict[str, float] = {}
        why: dict[str, list[str]] = {}
        for t in terms:
            for w in difflib.get_close_matches(t, by_word, n=5, cutoff=0.75):
                r = 1.0 if w == t else difflib.SequenceMatcher(None, t, w).ratio()
                for p in by_word[w]:
                    score[p] = score.get(p, 0) + r
                    why.setdefault(p, []).append(t if w == t else f"{t}~{w}")
        ranked = sorted(score, key=lambda p: (-score[p], p))[:limit]
        out = []
        for p in ranked:
            _, m = titles[p]
            out.append({"path": p, "title": m["fm"].get("title"), "type": m["fm"].get("type"), "scope": m["fm"].get("scope"),
                        "score": round(score[p], 2), "matched": "close spelling: " + ", ".join(why[p]), "snippet": m["summary"]})
        return out

    # --- writing -------------------------------------------------------------------------
    def append(self, page: str, text: str, summary: str, heading: str | None = None) -> dict:
        """Add a dated section to an existing page without resending the whole page."""
        p, name = self._page(page)
        if p in ("index", "log") or self._generated(p):
            raise WikiError(f"{p} is maintained automatically; append to the underlying page instead")
        if not (summary or "").strip():
            raise WikiError("a one-line summary of the change is required (it goes in the log)")
        if not (text or "").strip():
            raise WikiError("nothing to append")
        title = " ".join((heading or "Update").strip().lstrip("#").split())[:120]
        with self._lock:      # read, build and store as one step: two concurrent appends must not lose either
            old = self.store.read(name)
            if old is None:
                raise WikiError(f"no such page: {p} (use wiki_write to create it)")
            body = FRONTMATTER.sub("", old, count=1).rstrip() + f"\n\n## {title} ({self._today()})\n\n{text.strip()}\n"
            if len(body.encode("utf-8")) > MAX_BYTES:
                raise WikiError(f"page would exceed {MAX_BYTES} bytes; split it into a linked page instead")
            return self._store(p, name, parse_frontmatter(old), body, summary)

    def update_record(self, page: str, status: str | None = None, priority: str | None = None, due: str | None = None,
                      horizon: str | None = None, note: str | None = None) -> dict:
        """Change status, priority or due on a task, or status or horizon on a future need, adding a dated history line.
        A field that belongs to the other record type is refused, not silently stored."""
        p, name = self._page(page)
        requested = {k: v for k, v in (("status", status), ("priority", priority), ("due", due), ("horizon", horizon)) if v is not None}
        with self._lock:      # read, change and store as one step, like append
            old = self.store.read(name)
            if old is None:
                raise WikiError(f"no such page: {p}")
            fm = parse_frontmatter(old)
            rtype = fm.get("type")
            if rtype not in (records.TASK_TYPE, records.NEED_TYPE):
                raise WikiError(f"{p} is not a task or future need")
            allowed = records.UPDATABLE[rtype]
            wrong = sorted(set(requested) - set(allowed))
            if wrong:
                raise WikiError(f"a {rtype.replace('_', ' ')} has no {', '.join(wrong)}; it takes {', '.join(allowed)}")
            changes, new = [], dict(fm)
            for key, value in requested.items():
                if value != fm.get(key):
                    changes.append(f"{key}: {fm.get(key) or '(none)'} -> {value or '(cleared)'}")
                    new[key] = value
            if not changes and not (note or "").strip():
                raise WikiError("nothing to change")
            try:
                new, _ = records.validate(new, strict=True)
            except records.RecordError as exc:
                raise WikiError(str(exc)) from None
            body = FRONTMATTER.sub("", old, count=1).rstrip()
            clean_note = " ".join((note or "").split())
            line = f"- {self._today()}: " + ("; ".join(changes) if changes else "note") + (f". {clean_note}" if clean_note else "")
            body += ("\n" if "\n## History\n" in body else "\n\n## History\n\n") + line + "\n"
            r = self._store(p, name, new, body, (changes[0] if changes else "note") if len(changes) <= 1 else f"{len(changes)} field changes")
        return {**r, "changes": changes}

    def revision(self, page: str) -> str | None:
        """A short content hash of the page as stored (not including live portal lists added on read), or None if
        absent. wiki_read returns it; wiki_write(expected_revision=...) refuses to overwrite a page that changed since."""
        _, name = self._page(page)
        text = self.store.read(name)
        return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    def write(self, page: str, content: str, summary: str, expected_revision: str | None = None) -> dict:
        p, name = self._page(page)
        if p in ("index", "log") or self._generated(p):
            raise WikiError(f"{p} is maintained automatically (map, area hubs, index, log); write the underlying page instead")
        if not (summary or "").strip():
            raise WikiError("a one-line summary of the change is required (it goes in the log)")
        if len(content.encode("utf-8")) > MAX_BYTES:
            raise WikiError(f"page too large (limit {MAX_BYTES} bytes)")
        if not FRONTMATTER.match(content):
            raise WikiError("page must start with YAML frontmatter (--- ... ---)")
        content = _strip_live_blocks(content)          # a page read earlier carries computed lists; never store them
        fm = parse_frontmatter(content)
        missing = [k for k in REQUIRED if k not in fm and k != "updated"]
        if missing:
            raise WikiError(f"frontmatter is missing: {', '.join(missing)}")
        # Types first: a title that is a number or a list was stored, then crashed every later index rebuild in the silo.
        if not isinstance(fm["title"], str) or not fm["title"].strip():
            raise WikiError("title must be a non-empty string")
        if "type" in fm and not isinstance(fm["type"], str):
            raise WikiError("type must be a string")
        if not isinstance(fm["scope"], str) or fm["scope"] not in SCOPES:
            raise WikiError(f"scope must be one of {sorted(SCOPES)}")
        if not isinstance(fm["source_ids"], list):
            raise WikiError("source_ids must be a list (use e.g. [\"claude-session:2026-10-27\"] for new knowledge)")
        try:
            employee.validate(p, fm)
        except employee.EmployeeError as exc:
            raise WikiError(str(exc)) from None
        if fm.get("type") in (records.TASK_TYPE, records.NEED_TYPE):
            try:
                fm, _ = records.validate(fm, strict=True)
            except records.RecordError as exc:
                raise WikiError(str(exc)) from None
        body = FRONTMATTER.sub("", content, count=1)
        with self._lock:
            if expected_revision is not None:   # optimistic concurrency: checked under the write lock
                current = self.revision(p)
                if current != expected_revision.strip():
                    raise WikiError(f"revision_conflict: {p} changed since you read it (current revision "
                                    f"{current or 'none: the page does not exist'}); re-read it, merge your change, and write again")
            result = self._store(p, name, fm, body, summary)
            return {**result, "revision": self.revision(p)}

    def _store(self, p: str, name: str, fm: dict, body: str, summary: str) -> dict:
        person = p.startswith("people/")
        clean_body, counts = scrub(body, keep_emails=person, keep_phones=person and self._personal)
        fm, fm_counts = self._scrub_frontmatter(fm, keep_emails=person, keep_phones=person and self._personal)
        counts.update(fm_counts)
        prior = self.store.read(name)
        existed = prior is not None
        # Activity dates survive rewrites (a bulk re-index must not move them). A malformed new value is dropped (the
        # old one, if any, comes back below); an empty one does not defeat the old one. Setting a different VALID date
        # on purpose is allowed: the writer knows better than the server.
        fm = {k: v for k, v in fm.items() if k not in dates.DATE_FIELDS or dates.good(v)}
        if existed:
            old = parse_frontmatter(prior)
            fm = {**fm, **{k: old[k] for k in dates.DATE_FIELDS if old.get(k) and not fm.get(k)}}
        elif not dates.page_date(fm):          # only a page that did not exist is stamped; an old undated page stays undated
            fm = {**fm, "created": self._today()}
        fm = {**fm, "updated": self._today()}
        text = frontmatter(fm) + clean_body.lstrip("\n")
        if len(text.encode("utf-8")) > MAX_BYTES:       # the one place every stored page passes, whoever built it
            raise WikiError(f"page {p} would exceed {MAX_BYTES} bytes; split it into linked pages")
        self._check_quota(existed, prior, text)
        self.store.write(name, text)
        warnings = [f"link to a page that does not exist: [[{t}]]" for t in dict.fromkeys(m.group(1).strip() for m in LINK.finditer(clean_body))
                    if not self._exists(t)]
        warnings += [f"{rel} points to a page that does not exist: {t}" for rel, t in relation_targets(fm) if not self._exists(t)]
        if self._deferred is not None:      # inside a batch: one index rebuild and one log write at the end
            self._deferred.append(("update" if existed else "create", p, summary))
        else:
            self.rebuild_index()
            self._log("update" if existed else "create", p, summary)
        return {"path": p, "action": "updated" if existed else "created", "warnings": warnings, "scrubbed": dict(counts)}

    def _check_quota(self, existed: bool, prior: str | None, text: str) -> None:
        """Refuse a NEW page past the page limit, and any write that grows the silo past the byte limit. Updates that
        keep or shrink a page always pass, so a full silo can still be edited and tidied."""
        content = [name for p, name in self._all() if not self._generated(p)]      # the index, log and hubs do not count
        if not existed and len(content) >= self._max_pages:
            raise WikiError(f"this silo has reached its page limit ({self._max_pages} pages); merge or remove pages before adding more")
        growth = len(text) - len(prior or "")
        if growth > 0:
            total = sum(len(self.store.read(n) or "") for n in content)
            if total + growth > self._max_bytes:
                raise WikiError(f"this silo would pass its size limit ({self._max_bytes:,} characters of pages); merge or remove pages first")

    @staticmethod
    def _scrub_frontmatter(fm: dict, keep_emails: bool, keep_phones: bool = False) -> tuple[dict, Counter]:
        """Frontmatter values pass the same scrubber as the body: strings, lists, and dict values at any depth up to
        MAX_FM_DEPTH. Every source id must be a short plain identifier; a bad one refuses the write."""
        counts: Counter = Counter()

        def walk(v, depth: int):
            if depth > MAX_FM_DEPTH:
                raise WikiError("frontmatter is nested too deeply")
            if isinstance(v, str):
                v, c = scrub(v, keep_emails=keep_emails, keep_phones=keep_phones)
                counts.update(c)
                return v
            if isinstance(v, list):
                return [walk(x, depth + 1) for x in v]
            if isinstance(v, dict):
                for k in v:
                    # Keys are stored verbatim like values are scrubbed: a key is a label, never a place for contact details.
                    if not isinstance(k, str) or len(k) > 80 or any(scrub(k)[1].values()):
                        raise WikiError("a frontmatter key may not contain an email, phone or ID number")
                return {k: walk(x, depth + 1) for k, x in v.items()}
            return v

        out = {}
        for k, v in fm.items():
            if not (isinstance(k, str) and FM_KEY.fullmatch(k)):
                raise WikiError(f"bad frontmatter key {str(k)[:40]!r}: use letters, digits, - and _ (up to 41 characters)")
            if k == "source_ids":
                if not isinstance(v, list):
                    raise WikiError("source_ids must be a list")
                for sid in v:
                    if not (isinstance(sid, str) and SOURCE_ID.fullmatch(sid)):
                        raise WikiError(f"bad source id {str(sid)[:60]!r}: use 1-200 characters from letters, digits and : / . _ @ -")
                    # Source ids are stored verbatim (they are references, not prose), so anything the scrubber would
                    # remove (an email, phone, SSN, card or labelled number) is refused rather than kept.
                    if any(scrub(sid)[1].values()):
                        raise WikiError("a source id may not contain an email, phone or ID number; cite the system of record instead")
                out[k] = v
            else:
                out[k] = walk(v, 0)
        return out, counts

    def _log(self, kind: str, page: str, summary: str) -> None:
        self._log_many([(kind, page, summary)])

    def _log_many(self, entries: list[tuple[str, str, str]]) -> None:
        if not entries:
            return
        old = self.store.read("log.md") or LOG_HEADER
        # One entry per line (a summary with line breaks could forge extra log entries), and scrubbed: the log is
        # readable through wiki_log, and a summary is free text like a page body.
        def line(s) -> str:
            return scrub(" ".join(str(s).split())[:200])[0]
        self.store.write("log.md", old + "".join(f"\n## [{self._today()}] {k} | {p} — {line(s)}\n" for k, p, s in entries))

    def rebuild_index(self) -> None:
        self._rebuild_hubs()          # hubs first, so the index lists them
        by_section: dict[str, list[str]] = {s: [] for s in SECTIONS}
        for p, name in self._all():
            m = self._meta(name)
            line = f"- {wikilink(p, m['fm'].get('title'))}"
            summary = m["summary"]
            by_section[p.split("/")[0]].append(line + (f" — {summary}" if summary else ""))
        out = ['---', 'title: "Index"', 'type: "index"', '---', "", "# Index", "",
               "Catalog of every page: link, one-line summary. Read this first, then drill into pages. Maintained on every write.", ""]
        for s in SECTIONS:
            if by_section[s]:
                out += [f"## {s.capitalize()} ({len(by_section[s])})", ""] + sorted(by_section[s]) + [""]
        self.store.write("index.md", "\n".join(out))

    # --- generated navigation: the small map and one hub per area (progressive disclosure) --
    @staticmethod
    def _generated(p: str) -> bool:
        return p in (START, PEOPLE_HUB, records.HUB) or p.startswith(AREA_PREFIX)

    def _write_if_changed(self, name: str, text: str) -> None:
        if self.store.read(name) != text:
            self.store.write(name, text)

    def _rebuild_hubs(self) -> None:
        """reference/start-here (the map an agent reads first) and reference/area-<slug> (one per project area).
        Generated from project frontmatter and links, rewritten only when their content changes."""
        pages = [(p, n) for p, n in self._all() if not self._generated(p)]
        linked: dict[str, int] = {}
        for _, n in pages:
            m = self._meta(n)
            for t in set(m["links"]) | {t for _, t in m["relations"]}:
                linked[t] = linked.get(t, 0) + 1
        areas: dict[str, list[tuple[str, dict]]] = {}
        for p, n in pages:
            if p.startswith("projects/"):
                m = self._meta(n)
                areas.setdefault(str(m["fm"].get("area") or "Unsorted"), []).append((p, m))
        today = self._today()
        hub_links = []
        # Distinct labels can share a slug ("R&D", "R D"). A hub keeps the slug it already has, so adding a colliding
        # area never moves an existing hub (links to it would break); only an area with no hub yet takes a free slug.
        held: dict[str, str] = {}
        for name in self.store.names():
            if name.startswith(AREA_PREFIX) and name.endswith(".md"):
                title = str(parse_frontmatter(self.store.read(name) or "").get("title", ""))
                if title.startswith("Area: ") and title[6:] in areas:
                    held.setdefault(title[6:], name[len(AREA_PREFIX):-3])
        used: set[str] = set(held.values())
        for area in sorted(areas, key=lambda a: (a == "Unsorted", a.lower())):
            projs = areas[area]
            if area in held:
                slug = held[area]
            else:
                base = slug = slugify(area) or "area"
                n = 2
                while slug in used:
                    slug, n = f"{base}-{n}", n + 1
                used.add(slug)
            active = [x for x in projs if str(x[1]["fm"].get("status", "")).lower() in ACTIVE]
            rest = [x for x in projs if x not in active]

            def row(x):
                p, m = x
                status = m["fm"].get("status") or "no status"
                return f"- {wikilink(p, m['fm'].get('title'))} ({status}; {linked.get(p, 0)} linked pages)" + (f": {m['summary']}" if m["summary"] else "")
            body = [f"# Area: {area}", "",
                    ("Every project in this area, generated from project pages (do not edit; edit the projects). "
                     f"Back to [[{START}|Start here]]."), ""]
            if active:
                body += [f"## Active ({len(active)})", "", *sorted(map(row, active)), ""]
            if rest:
                body += [f"## Other ({len(rest)})", "", *sorted(map(row, rest)), ""]
            scopes = {str(m["fm"].get("scope")) for _, m in projs}
            scope = scopes.pop() if len(scopes) == 1 else "personal"
            fm = {"title": f"Area: {area}", "type": "hub", "scope": scope, "source_ids": ["generated"], "updated": today}
            self._write_if_changed(f"{AREA_PREFIX}{slug}.md", frontmatter(fm) + "\n".join(body))
            hub_links.append(f"- {wikilink(AREA_PREFIX + slug, area)}: {len(projs)} projects, {len(active)} active")
        people = sorted(((p, self._meta(n)) for p, n in pages if p.startswith("people/")), key=lambda x: str(x[1]["fm"].get("title", "")).lower())
        if people:
            def prow(x):
                p, m = x
                bits = [str(b) for b in (m["fm"].get("role"), m["fm"].get("org")) if b]
                return f"- {wikilink(p, m['fm'].get('title'))}" + (f": {', '.join(bits)}" if bits else "") + f" ({linked.get(p, 0)} linked pages)"
            kept = "email addresses and phone numbers; IDs stay in Contacts" if self._personal else "email addresses; phones and IDs stay in Contacts"
            pbody = ["# People", "", ("Everyone with a person page, generated (do not edit; edit the person page). Person pages may hold "
                                      f"{kept}. Back to [[{START}|Start here]]."), "", *map(prow, people), ""]
            pfm = {"title": "People", "type": "hub", "scope": "personal", "source_ids": ["generated"], "updated": today}
            self._write_if_changed(f"{PEOPLE_HUB}.md", frontmatter(pfm) + "\n".join(pbody))
            hub_links.append(f"- {wikilink(PEOPLE_HUB, 'People')}: {len(people)} person pages")
        overviews = sorted(((p, self._meta(n)) for p, n in pages if p.startswith("concepts/")),
                           key=lambda x: str(x[1]["fm"].get("title", "")).lower())
        overview_block = (["## Topic overviews", "", "Start here for a subject: each page synthesizes every note on it, with sources.", "",
                           *[f"- {wikilink(p, m['fm'].get('title'))}" for p, m in overviews], ""] if overviews else [])
        hub_text, n_open = records.hub(self)
        hfm = {"title": "Tasks and future needs", "type": "hub", "scope": "personal", "source_ids": ["generated"], "updated": today}
        self._write_if_changed(f"{records.HUB}.md", frontmatter(hfm) + hub_text)
        hub_links.append(f"- {wikilink(records.HUB, 'Tasks and future needs')}: {n_open} open tasks")
        counts = {s: sum(1 for p, _ in pages if p.startswith(s + "/")) for s in SECTIONS}
        n_generated = len(hub_links) + 1          # each area hub (and People) plus this map
        first = (["## First", "", "- [[reference/profile|Your profile]]: how you want this brain to work for you.", ""]
                 if self.store.read("reference/profile.md") is not None else [])
        body = ["# Start here", "",
                ("The map of this silo. Read this, then follow one link at a time and read only the page you "
                 "need. Generated; do not edit."), "",
                *first,
                *employee.start_block(self),
                *overview_block,
                "## Areas", "", *hub_links, "",
                "## Sections", "",
                *[f"- {s}: {c} pages (`wiki_index(section=\"{s}\")`)" for s, c in counts.items() if c],
                f"- plus {n_generated} generated hubs (this map, the area hubs and People), which the index also lists.", "",
                "## How to walk it", "",
                "- Topic question: `wiki_context(topic)` gives the best pages and their neighbours.",
                "- A project: its area hub, then the project page, then only the notes or sessions it links to.",
                "- Connections: `wiki_links(path)` shows links, backlinks and typed relations.",
                "- Recent activity: `wiki_log`.", ""]
        fm = {"title": "Start here", "type": "hub", "scope": "personal", "source_ids": ["generated"], "updated": today}
        self._write_if_changed(f"{START}.md", frontmatter(fm) + "\n".join(body))

    # --- session ingest (replaces /notion-import for knowledge) --------------------------
    def _unique(self, section: str, slug: str) -> str:
        path, n = f"{section}/{slug}", 2
        while self.store.read(f"{path}.md") is not None:
            path, n = f"{section}/{slug}-{n}", n + 1
        return path

    def _note_page(self, title: str) -> tuple[str, str | None]:
        """(path, existing text or None) for a note title. Titles that share a slug ("C++" and "C#" both become `c`)
        are separate pages (`c`, `c-2`, ...): find the page whose title matches, else the first free path, so a
        repeated title updates its own page instead of minting `c-3`, `c-4`."""
        slug = slugify(title)
        path, n = f"notes/{slug}", 2
        while True:
            old = self.store.read(f"{path}.md")
            if old is None or str(parse_frontmatter(old).get("title", "")).strip().lower() == title.lower():
                return path, old
            path, n = f"notes/{slug}-{n}", n + 1

    def _project_index(self) -> dict[str, str]:
        out = {}
        for p, name in self._all():
            if p.startswith("projects/"):
                out[self._meta(name)["title_low"].strip()] = p
        return out

    def ingest_session(self, payload: dict) -> dict:
        """Create the session page, new notes and any new projects from the canonical session JSON.

        Tasks and future needs become records (`tasks/`, `future-needs/`, deduped by title); maintenance items and
        captures are listed as follow-ups on the session page. Nothing in the payload is dropped silently: select
        values outside records.ALLOWED are omitted and reported in `dropped_values`.
        """
        ignored = _check_payload(payload)
        s = payload.get("session") or {}
        if not s.get("summary"):
            raise WikiError("session.summary is required")
        with self._lock:
            # All or nothing: every page goes to a staging layer first and reaches the store only if the whole ingest
            # succeeded. A late failure (an oversized note, a bad value) leaves the silo exactly as it was.
            real = self._base_store
            layer = _Staged(real)
            self._staging.store = layer           # this thread only: other threads keep reading the real store
            self._deferred = []
            try:
                result = self._ingest(payload, s)
                staged = layer.pending
            finally:
                self._staging.store = None
                entries, self._deferred = self._deferred, None
            for name, text in staged.items():
                real.write(name, text)
            if entries:
                self.rebuild_index()
                self._log_many(entries)
            result["ignored_fields"] = ignored
            result["pages_to_review"] = self._review_candidates(payload, set(result["created"]) | set(result["updated"]) | {result["session"]})
            return result

    def _review_candidates(self, payload: dict, touched: set[str]) -> list[dict]:
        """Existing pages a session probably changes: search hits on each new note title and project. Ingest
        only appends; integrating the new facts into these pages (wiki_append or wiki_write) is the agent's job."""
        seen, out = set(touched), []
        queries = [n.get("title") for n in payload.get("notes") or []] + [p.get("name") for p in payload.get("projects") or []]
        queries.append((payload.get("session") or {}).get("concerning"))
        for q in (x for x in queries if x):
            try:
                hits = self.search(q, 3)
            except WikiError:
                continue
            for h in hits:
                if h["path"] not in seen:
                    seen.add(h["path"])
                    out.append({"path": h["path"], "title": h["title"], "because": q})
        return out[:15]

    def _ingest(self, payload: dict, s: dict) -> dict:
        date = s.get("date") or self._today()
        title = s.get("title") or f"Daily chat on {date}"
        projects = self._project_index()
        created: list[str] = []
        updated: list[str] = []

        scrubbed: Counter = Counter()

        def note(result: dict) -> None:
            (created if result["action"] == "created" else updated).append(result["path"])
            scrubbed.update(result.get("scrubbed") or {})

        for proj in payload.get("projects") or []:
            name = _clean(proj.get("name"))
            if name and name.lower() not in projects:
                path = self._unique("projects", slugify(name))
                scope = "personal" if proj.get("type") == "Personal" else "work"
                body = (f"# {name}\n\n{proj.get('summary') or ''}\n\n- **Status:** {proj.get('status') or 'unknown'}\n- **Type:** {proj.get('type') or 'unknown'}\n")
                area = (proj.get("area") or "").strip()
                r = self._store(path, f"{path}.md", {"title": name, "type": "project", "scope": scope, "status": proj.get("status"),
                                                      **({"area": area} if area else {}),
                                                      "source_ids": [f"claude-session:{date}"]}, body, f"new project from session {date}")
                projects[name.lower()] = path
                note(r)

        def link(name: str | None) -> str:
            path = projects.get((name or "").strip().lower())
            return wikilink(path, name) if path else (name or "")

        def scope_of(name: str | None) -> str:
            path = projects.get((name or "").strip().lower())
            return parse_frontmatter(self.store.read(f"{path}.md") or "").get("scope", "unscoped") if path else "unscoped"

        for n in payload.get("notes") or []:
            ntitle = _clean(n.get("title"))      # the stored title is scrubbed, so match and slug on the scrubbed one
            if not ntitle:
                continue
            path, old = self._note_page(ntitle)
            name = f"{path}.md"
            text = (n.get("body") or "").strip()
            if old is not None:
                fm = parse_frontmatter(old)
                note(self._store(path, name, fm, FRONTMATTER.sub("", old, count=1).rstrip() + f"\n\n## Update {date}\n\n{text}\n", f"update from session {date}"))
            else:
                project = n.get("project")
                body = f"# {ntitle}\n\n" + (f"Project: {link(project)}\n\n" if project else "") + text + "\n"
                note(self._store(path, name, {"title": ntitle, "type": "note", "scope": scope_of(project), "tags": n.get("tag"), "source_ids": [f"claude-session:{date}"]},
                                 body, f"new note from session {date}"))

        concerning = s.get("concerning")
        path = self._unique("sessions", slugify(_clean(f"{date} {concerning or title}")))
        parts = [s["summary"].strip(), ""]
        related = [x for x in [concerning, *(s.get("cross_related_projects") or [])] if x]
        if related:
            parts += ["## Projects", *[f"- {link(x)}" for x in dict.fromkeys(related)], ""]
        counts = {}
        rec = self._ingest_records(payload, path, projects, scope_of, date)
        created += rec["created"]
        updated += rec["updated"]
        counts["tasks"], counts["future_needs"] = rec["tasks"], rec["future_needs"]
        if rec["links"]:
            parts += ["## Tasks and future needs from this session", *rec["links"], ""]
        for key, label, fmt in (("maintenance", "Recurring maintenance", lambda t: f"{t.get('name')} ({t.get('cadence') or 'no cadence'})"),
                                ("captures", "Captures", lambda t: f"{t.get('capture')} ({t.get('type') or 'Note'})")):
            items = payload.get(key) or []
            counts[key] = len(items)
            if items:
                parts += [f"## {label} (follow-ups)", *[f"- {fmt(i)}" for i in items], ""]
        r = self._store(path, f"{path}.md", {"title": title, "type": "session", "scope": scope_of(concerning), "date": date,
                                              "source_ai": s.get("source_ai") or "Claude", "source_ids": [f"claude-session:{date}"]},
                        f"# {title}\n\n" + "\n".join(parts), f"session {date}: {(concerning or title)[:80]}")
        note(r)
        return {"session": path, "created": created, "updated": updated, "follow_ups": counts, "scrubbed": dict(scrubbed + rec["scrubbed"]),
                "dropped_values": rec["dropped"]}

    def _ingest_records(self, payload: dict, session: str, projects: dict, scope_of, date: str) -> dict:
        """Create task and future-need pages from a session payload. Dedupe by title (case-insensitive) within each
        kind: an existing record keeps its values and only gains fields it lacked. Unknown select values are
        omitted and reported, never invented."""
        existing = {records.TASK_TYPE: {}, records.NEED_TYPE: {}}
        for p, name in self._all():
            m = self._meta(name)
            rtype_ = m["fm"].get("type")
            if isinstance(rtype_, str) and rtype_ in existing:      # a page with an unhashable type must not break ingest
                existing[rtype_][m["title_low"].strip()] = (p, name)
        out = {"created": [], "updated": [], "links": [], "dropped": [], "tasks": 0, "future_needs": 0, "scrubbed": Counter()}
        specs = [(records.TASK_TYPE, records.TASKS, "session", payload.get("tasks") or [],
                  lambda t: {"title": t.get("task"), "status": t.get("status"), "priority": t.get("priority"), "kind": t.get("type"),
                             "due": t.get("due"), "project": t.get("project"), "notes": t.get("notes")}),
                 (records.NEED_TYPE, records.NEEDS, "origin_session", payload.get("future_needs") or [],
                  lambda f: {"title": f.get("name"), "status": f.get("status"), "horizon": f.get("horizon"),
                             "project": f.get("related_project") or f.get("project"), "notes": f.get("notes")})]
        for rtype, section, srel, items, norm in specs:
            for raw in items:
                d = norm(raw)
                title = _clean(d.get("title"))
                if not title:
                    continue
                out["tasks" if rtype == records.TASK_TYPE else "future_needs"] += 1
                proj_path = projects.get((d.get("project") or "").strip().lower())
                fm = {"title": title, "type": rtype, "scope": scope_of(d.get("project")),
                      **{k: d.get(k) for k in ("status", "priority", "kind", "due", "horizon") if d.get(k) not in (None, "")},
                      **({"project": proj_path} if proj_path else {}), srel: session, "source_ids": [f"claude-session:{date}"]}
                fm, dropped = records.validate(fm, strict=False)
                out["dropped"] += [f"{title}: {x}" for x in dropped]
                hit = existing[rtype].get(title.lower())
                if hit:
                    p, name = hit
                    old = parse_frontmatter(self.store.read(name) or "")
                    merged = {**old, **{k: v for k, v in fm.items() if k not in old and k != "source_ids"}}
                    body = FRONTMATTER.sub("", self.store.read(name) or "", count=1).rstrip()
                    if d.get("notes"):
                        body += f"\n\n## Update {date}\n\n{d['notes'].strip()}"
                    r = self._store(p, name, merged, body + "\n", f"task update from session {date}")
                    out["updated"].append(r["path"])
                    out["scrubbed"].update(r.get("scrubbed") or {})
                else:
                    p = self._unique(section, slugify(title))
                    body = f"# {title}\n\n" + ((d.get("notes") or "").strip() + "\n" if d.get("notes") else "")
                    r = self._store(p, f"{p}.md", fm, body, f"new {rtype.replace('_', ' ')} from session {date}")
                    existing[rtype][title.lower()] = (p, f"{p}.md")
                    out["created"].append(r["path"])
                    out["scrubbed"].update(r.get("scrubbed") or {})
                out["links"].append(f"- {wikilink(r['path'], title)}")
        return out



class _Staged:
    """A write buffer over a Store: reads see staged writes first, nothing reaches the real store until the caller
    flushes `pending`. ingest_session runs on one so a failed ingest writes nothing."""

    def __init__(self, store):
        self._store, self.pending = store, {}

    def names(self) -> list[str]:
        return sorted(set(self._store.names()) | set(self.pending))

    def read(self, name: str) -> str | None:
        return self.pending[name] if name in self.pending else self._store.read(name)

    def write(self, name: str, text: str) -> None:
        self.pending[name] = text


MAX_ITEMS = 200                         # per payload array: one ingest must not write unbounded pages
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# The fields ingest reads, per object. Anything else is reported back in `ignored_fields`, never silently dropped.
FIELDS = {
    "meta": {"source", "context", "default_type"},
    "session": {"title", "date", "source_ai", "import_status", "summary", "concerning", "cross_related_projects", "store_raw_json"},
    "projects": {"name", "summary", "type", "status", "area"},
    "notes": {"title", "tag", "project", "body"},
    "tasks": {"task", "project", "priority", "status", "due", "type", "notes"},
    "future_needs": {"name", "horizon", "status", "related_project", "project", "notes"},
    "maintenance": {"name", "cadence", "category", "automation", "priority", "project"},
    "captures": {"capture", "type", "project"},
}
ITEM_KEY = {"projects": "name", "notes": "title", "tasks": "task", "future_needs": "name", "maintenance": "name", "captures": "capture"}
LIST_FIELDS = {"cross_related_projects", "tag"}
BOOL_FIELDS = {"store_raw_json"}
KNOWN_TOP = set(FIELDS) | {"repos"}     # `repos` is part of the canonical JSON but not used by the wiki


def _check_payload(payload) -> list[str]:
    """Refuse a malformed session payload BEFORE anything is staged, and list the fields ingest will not use.

    Refused: a non-object payload or item, an array that is not a list or has more than MAX_ITEMS items, a value of the
    wrong type, an item without its identifying field (a note without `title`), and a `session.date` that is not
    YYYY-MM-DD. Reported (returned): unknown keys, so a misspelled `bdy` is visible instead of an empty note."""
    if not isinstance(payload, dict):
        raise WikiError("payload must be an object")
    ignored = [k for k in payload if k not in KNOWN_TOP]

    def check(obj: dict, kind: str, where: str) -> None:
        for k, v in obj.items():
            if k not in FIELDS[kind]:
                ignored.append(f"{where}.{k}")
                continue
            ok = (v is None or isinstance(v, str)
                  or (k in LIST_FIELDS and isinstance(v, list) and all(isinstance(x, str) for x in v))
                  or (k in BOOL_FIELDS and isinstance(v, bool)))
            if not ok:
                raise WikiError(f"{where}.{k} must be " + ("a list of strings" if k in LIST_FIELDS else "true or false" if k in BOOL_FIELDS else "a string"))

    for kind in ("meta", "session"):
        obj = payload.get(kind)
        if obj is None:
            continue
        if not isinstance(obj, dict):
            raise WikiError(f"{kind} must be an object")
        check(obj, kind, kind)
    date = (payload.get("session") or {}).get("date")
    if date not in (None, "") and not DATE_RE.fullmatch(date):
        raise WikiError(f"session.date must be YYYY-MM-DD, not {str(date)[:20]!r}")
    for kind, key in ITEM_KEY.items():
        items = payload.get(kind)
        if items is None:
            continue
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise WikiError(f"{kind} must be a list of objects")
        if len(items) > MAX_ITEMS:
            raise WikiError(f"{kind}: at most {MAX_ITEMS} items per ingest")
        for n, item in enumerate(items):
            check(item, kind, f"{kind}[{n}]")
            if not str(item.get(key) or "").strip():
                raise WikiError(f"{kind}[{n}] needs a {key}")
    return ignored
