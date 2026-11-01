"""The multi-user brain: one `Wiki` per silo, with every call checked against the caller's identity.

Each silo is a separate wiki on its own store prefix, so a user's index, hubs, search and links never include
pages from a silo they cannot read: isolation is structural, not a filter over one shared index. Cross-silo
recall (`context`, `search`) fans out over the caller's readable silos only and labels every result with its silo.
Every check, allowed or refused, goes to the audit callback.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable

from mcp_server import dates
from mcp_server.access import Identity, can_read, can_write, readable, resolve
from mcp_server.store import Store
from mcp_server.wiki_store import Wiki, WikiError

# Per-call result caps. context() runs links() over every page for every hit, so an unbounded limit let one call
# monopolize the (single) instance: 60 pages at limit 60 took 9.7 s.
MAX_CONTEXT, MAX_SEARCH, MAX_LIST, MAX_PER_DAY, MAX_LOG = 20, 50, 500, 100, 200
# Audit lines hold caller-supplied path and silo strings. Only slug-like values are logged as given; anything else (an
# email, page text, a newline) becomes a fixed marker, so the log never carries personal data or forged lines.
SAFE_TOKEN = re.compile(r"[a-z0-9/_-]{0,120}")
# A slug can still spell a phone number or SSN ("notes/555-123-4567", "notes/123456789"), since page slugs come from
# titles. Mirrors the scrubber's phone and SSN shapes, with `-`/`_` separators and runs without separators too. It
# over-redacts a 9 or 10 digit id on purpose: an audit line needs to say who and what kind, not carry a possible number.
PII_SHAPED = re.compile(r"(?<!\d)(?:(?:1[-_]?)?\d{3}[-_]?\d{3}[-_]?\d{4}|\d{3}[-_]?\d{2}[-_]?\d{4})(?!\d)")


def safe(value) -> str:
    v = "" if value is None else str(value)
    if not SAFE_TOKEN.fullmatch(v):
        return "<invalid>"
    return "<redacted>" if PII_SHAPED.search(v) else v


def clamp(n, hi: int) -> int:
    try:
        return max(1, min(int(n), hi))
    except (TypeError, ValueError):
        return 1


class Brain:
    def __init__(self, store_for: Callable[[str], Store], today: Callable[[], str] | None = None,
                 audit: Callable[[dict], None] | None = None):
        self._store_for = store_for
        self._today = today
        self._wikis: dict[str, Wiki] = {}
        self._wikis_lock = threading.Lock()      # tools run in worker threads: one Wiki (one write lock) per silo
        self._audit = audit or (lambda event: None)

    def _wiki(self, silo: str) -> Wiki:
        with self._wikis_lock:
            if silo not in self._wikis:
                self._wikis[silo] = Wiki(self._store_for(silo), today=self._today, personal=silo.startswith("users/"))
            return self._wikis[silo]

    def _check(self, identity: Identity, silo: str | None, write: bool, action: str, path: str = "") -> str:
        try:
            s = resolve(identity, silo)
        except PermissionError:
            # A malformed silo name is still an attempt: audit it (as a safe marker if it is not slug-like) before refusing.
            self._audit({"user_id": identity.user_id, "action": action, "silo": safe(silo), "path": safe(path), "allowed": False})
            raise PermissionError(f"no access to silo {str(silo)[:80]!r}") from None
        allowed = can_write(identity, s) if write else can_read(identity, s)
        self._audit({"user_id": identity.user_id, "action": action, "silo": s, "path": safe(path), "allowed": allowed})
        if not allowed:
            # Same message whether the silo exists or not: no probing of other silos.
            raise PermissionError(f"no access to silo {s!r}")
        return s

    # --- reads ---------------------------------------------------------------------------------
    def read(self, identity: Identity, path: str, silo: str | None = None) -> dict:
        s = self._check(identity, silo, False, "read", path)
        w = self._wiki(s)
        return {"silo": s, "path": path, "content": w.read(path), "revision": w.revision(path)}

    def index(self, identity: Identity, silo: str | None = None, section: str | None = None, offset: int = 0, limit: int = 300) -> dict:
        s = self._check(identity, silo, False, "index", section or "")
        return {"silo": s, "index": self._wiki(s).index_view(section, offset, limit)}

    def search(self, identity: Identity, query: str, limit: int = 10) -> dict:
        """Keyword search over the caller's readable silos. A YYYY-MM-DD in the query is an exact-date filter per silo."""
        limit = clamp(limit, MAX_SEARCH)
        hits, notes, dated = [], [], []
        for s in readable(identity):
            self._audit({"user_id": identity.user_id, "action": "search", "silo": s, "path": "", "allowed": True})
            try:
                r = self._wiki(s).search_view(query, limit)
            except WikiError:
                continue
            hits += [{**h, "silo": s} for h in r["results"]]
            dated = r.get("dates") or dated
            if r.get("note") and (r.get("date_only") or not r["results"]):
                notes.append(f"{s}: {r['note']}")
        if dated:
            hits.sort(key=lambda h: (h.get("matched") != "dated that day", h["silo"], h["path"]))
        else:
            hits.sort(key=lambda h: -float(h.get("score", 0)))
        out = {"results": hits[:limit]}
        if dated:
            out["dates"] = dated
            if not hits:
                out["note"] = f"No entries for {', '.join(dated)} in the silos you can read. " + " ".join(notes)
            elif notes:
                out["note"] = " ".join(notes)           # silos where the extra words matched nothing on that date
        return out

    def context(self, identity: Identity, topic: str, limit: int = 5) -> dict:
        limit = clamp(limit, MAX_CONTEXT)
        out, notes = [], []
        dated = bool(dates.ISO.search(topic or ""))
        for s in readable(identity):
            self._audit({"user_id": identity.user_id, "action": "context", "silo": s, "path": "", "allowed": True})
            try:
                c = self._wiki(s).context(topic, limit)
            except WikiError:
                continue
            out += [{**r, "silo": s} for r in c["results"]]
            if c.get("note") and (c.get("date_only") or not c["results"]):
                notes.append(f"{s}: {c['note']}")
        if dated:
            out.sort(key=lambda r: (r.get("matched") != "dated that day", r["silo"], r["path"]))
        else:
            out.sort(key=lambda r: -float(r.get("score", 0)))
        hint = ("Read the pages that matter with wiki_read(path, silo); cite them as silo:path."
                if out else "Nothing on this in the silos you can read. Say so rather than guessing.")
        res = {"topic": topic, "results": out[:limit], "profile": f"{identity.home}:reference/profile", "hint": hint}
        if dated:
            res["dates"] = dates.ISO.findall(topic)
            if not out:
                res["note"] = f"No entries for {', '.join(res['dates'])} in the silos you can read. " + " ".join(notes)
            elif notes:
                res["note"] = " ".join(notes)
        return res

    def list(self, identity: Identity, silo: str | None = None, section: str | None = None, scope: str | None = None, limit: int = 200,
             date_from: str | None = None, date_to: str | None = None, sort: str = "path", status: str | None = None,
             not_status: str | None = None) -> dict:
        limit = clamp(limit, MAX_LIST)
        s = self._check(identity, silo or identity.home, False, "list", section or "")
        return {"silo": s, **self._wiki(s).list_view(section, scope, limit, date_from, date_to, sort, status, not_status)}

    def timeline(self, identity: Identity, date_from: str, date_to: str, silo: str | None = None, scope: str | None = None,
                 section: str | None = None, per_day: int = 40, compact: bool = False) -> dict:
        per_day = clamp(per_day, MAX_PER_DAY)
        s = self._check(identity, silo or identity.home, False, "timeline", section or "")
        return {"silo": s, **self._wiki(s).timeline(date_from, date_to, scope, per_day, section, compact)}

    def log(self, identity: Identity, n: int = 10, since: str | None = None, until: str | None = None, silo: str | None = None) -> dict:
        s = self._check(identity, silo or identity.home, False, "log")
        return {"silo": s, **self._wiki(s).log_view(clamp(n, MAX_LOG), since, until)}

    def links(self, identity: Identity, path: str, silo: str | None = None) -> dict:
        s = self._check(identity, silo, False, "links", path)
        return {"silo": s, **self._wiki(s).links(path)}

    # --- writes --------------------------------------------------------------------------------
    def write(self, identity: Identity, path: str, content: str, summary: str, silo: str | None = None,
              expected_revision: str | None = None) -> dict:
        s = self._check(identity, silo, True, "write", path)
        return {"silo": s, **self._wiki(s).write(path, content, summary, expected_revision)}

    def append(self, identity: Identity, path: str, text: str, summary: str, heading: str | None = None, silo: str | None = None) -> dict:
        s = self._check(identity, silo, True, "append", path)
        return {"silo": s, **self._wiki(s).append(path, text, summary, heading)}

    def update_record(self, identity: Identity, path: str, status: str | None = None, priority: str | None = None,
                      due: str | None = None, horizon: str | None = None, note: str | None = None, silo: str | None = None) -> dict:
        s = self._check(identity, silo, True, "task_update", path)
        return {"silo": s, **self._wiki(s).update_record(path, status, priority, due, horizon, note)}

    def ingest_session(self, identity: Identity, payload: dict) -> dict:
        s = self._check(identity, None, True, "ingest")           # sessions always land in the caller's own silo
        return {"silo": s, **self._wiki(s).ingest_session(payload)}
