"""Date-aware lookup: "what was I working on around date X?", within one silo (the router decides which silos a caller may query).

A page's activity date is never `updated` (bulk rewrites stamp that on hundreds of pages at once). It is, in order:
`date` (sessions: the date in the title), `created` (set once when a page is first written, kept on every rewrite,
stamped by the server on first write), or the date at the end of the first dated source id (`claude-session:2026-10-28`,
`import:handbook:2026-10-27`, `raw/claude-memory/2026-10-27/profile.md`: when it was captured). Pages also carry dated
events in their body: task history lines (`- 2026-10-28: status: ...`) and appended or ingested sections
(`## Heading (2026-10-28)`, `## Update 2026-10-28`); the timeline reports those on the day they happened.
"""

from __future__ import annotations

import re

ISO = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
MAX_SOURCE_ID = 200
MAX_LINE = 300          # event lines longer than this are prose, not history; skipping them keeps parsing linear
HISTORY_LINE = re.compile(r"- (\d{4}-\d{2}-\d{2}): ([^\n]*)")
# "## Heading (2026-10-28)" (appended sections), or "## Update 2026-08-01" / "## Correction 2026-08-01 - why" (ingest).
DATED_HEADING = re.compile(
    r"## (?:(?P<h1>[^\n]*?)[ \t]*\((?P<d1>\d{4}-\d{2}-\d{2})\)[ \t]*"
    r"|(?P<h2>[A-Za-z][\w-]*)[ \t]+(?P<d2>\d{4}-\d{2}-\d{2})(?!\d)(?P<tail>[^\n]*))")
_SEG = re.compile(r"[A-Za-z0-9._-]+")
SORTS = ("date_desc", "date_asc", "updated", "path")
DATE_FIELDS = ("date", "created")       # kept on every rewrite, never re-stamped
TOTAL_CAP = 150                         # timeline items across the whole range


def valid(d: str | None, name: str) -> str | None:
    if d in (None, ""):
        return None
    if not (isinstance(d, str) and ISO.fullmatch(d)):
        raise ValueError(f"{name} must be YYYY-MM-DD, not {d!r}")
    return d


def good(v) -> bool:
    """Is v a usable date value (a string that starts YYYY-MM-DD)?"""
    return isinstance(v, str) and bool(ISO.fullmatch(v[:10]))


def page_date(fm: dict) -> str | None:
    """The activity date of a page (see the module docstring), or None."""
    for key in DATE_FIELDS:
        v = fm.get(key)
        if good(v):
            return v[:10]
    for sid in fm.get("source_ids") or []:
        d = source_date(sid)
        if d:
            return d
    return None


def source_date(sid) -> str | None:
    """The date in a dated source id, or None. The date is a whole ":"- or "/"-separated segment after the first one
    ("claude-session:2026-10-28", "private:personal-directory:2026-10-27", "raw/claude-memory/2026-10-27/profile.md");
    a bare Notion id or "x:notadate" has none. Plain code, not a regex: the old pattern backtracked badly on long ids."""
    s = str(sid)
    if not s or len(s) > MAX_SOURCE_ID or not s[0].isascii() or not s[0].isalpha() or not s[0].islower():
        return None
    parts = re.split(r"[:/]", s)
    if not all(_SEG.fullmatch(x) for x in parts):
        return None
    for seg in parts[1:]:
        if ISO.fullmatch(seg):
            return seg
    return None


def events(body: str) -> list[tuple[str, str, str]]:
    """Dated things in a page body after it was created: (date, what, kind).

    kind is "changed" for a true history line (`- 2026-10-28: status: ...` inside `## History`) and for an appended
    or ingested section heading (`## Heading (2026-10-28)`, `## Update 2026-10-28`); "dated" for a bullet of the
    same shape anywhere else (a note that merely lists dates)."""
    out, section = [], ""
    for line in body.splitlines():
        if line.startswith("## "):
            section = line[3:MAX_LINE].strip().lower()
            m = DATED_HEADING.fullmatch(line) if len(line) <= MAX_LINE else None
            if m:
                if m.group("d1"):
                    out.append((m.group("d1"), m.group("h1").strip(), "changed"))
                else:
                    out.append((m.group("d2"), (m.group("h2") + (m.group("tail") or "")).strip(), "changed"))
            continue
        m = HISTORY_LINE.fullmatch(line) if len(line) <= MAX_LINE else None
        if m:
            out.append((m.group(1), m.group(2).strip(), "changed" if section == "history" else "dated"))
    return out


def in_range(d: str | None, date_from: str | None, date_to: str | None) -> bool:
    if d is None:
        return False
    return (date_from is None or d >= date_from) and (date_to is None or d <= date_to)


def coverage(wiki) -> dict:
    """First and last activity date in this wiki, overall and for sessions (what "no match" should be compared to)."""
    all_d, sess = [], []
    for p, name in wiki._all():
        d = wiki._meta(name)["date"]
        if d:
            all_d.append(d)
            if p.startswith("sessions/"):
                sess.append(d)
    return {"first": min(all_d, default=None), "last": max(all_d, default=None),
            "sessions_first": min(sess, default=None), "sessions_last": max(sess, default=None)}


def coverage_line(cov: dict) -> str:
    if not cov["first"]:
        return "No page in this wiki carries an activity date yet."
    s = f"Dated pages span {cov['first']} to {cov['last']}"
    if cov["sessions_first"]:
        s += f"; sessions span {cov['sessions_first']} to {cov['sessions_last']}"
    return s + "."


def timeline(wiki, date_from: str, date_to: str, scope: str | None = None, per_day: int = 15, section: str | None = None,
             max_total: int = TOTAL_CAP, compact: bool = False) -> dict:
    """Everything dated inside [date_from, date_to], grouped by day: pages created or dated that day, and dated
    events (task history, appended sections) on any page. One item per page per day (several history lines are
    joined with "; "). At most per_day items a day and max_total in all; every day's count stays complete.
    Generated hubs are left out."""
    date_from, date_to = valid(date_from, "date_from"), valid(date_to, "date_to")
    if not date_from or not date_to or date_from > date_to:
        raise ValueError("give date_from and date_to as YYYY-MM-DD, with date_from on or before date_to")
    per_day, max_total = max(1, per_day), max(1, max_total)
    days: dict[str, list[dict]] = {}
    for p, name in wiki._all():
        if wiki._generated(p) or (section and not p.startswith(section + "/")):
            continue
        m = wiki._meta(name)
        fm = m["fm"]
        if scope and fm.get("scope") != scope:
            continue
        base = {"path": p, "title": fm.get("title"), "type": fm.get("type")}
        d = m["date"]
        if in_range(d, date_from, date_to):
            if p.startswith("sessions/"):
                kind, summary = "session", m["summary"]
            else:
                kind = "created (imported)" if fm.get("created_source") else "created"   # migration vs real creation
                summary = m["summary"][:120]
            days.setdefault(d, []).append({**base, "what": kind, "summary": summary})
        per_page: dict[str, list[tuple[str, str]]] = {}
        for ed, what, ekind in m["events"]:
            if in_range(ed, date_from, date_to) and ed != d:
                per_page.setdefault(ed, []).append((what, ekind))
        for ed, evs in per_page.items():
            kind = "changed" if any(k == "changed" for _, k in evs) else "dated"
            days.setdefault(ed, []).append({**base, "what": kind, "summary": "; ".join(w for w, _ in evs)[:160]})
    if compact:
        return _compact(wiki, date_from, date_to, days)
    order = {"session": 0, "created": 1, "created (imported)": 1, "changed": 2, "dated": 3}
    out, dropped, room = [], 0, max_total
    for day in sorted(days):
        items = sorted(days[day], key=lambda x: (order[x["what"]], str(x["title"]).lower()))
        take = min(per_day, room, len(items))
        dropped += len(items) - take
        room -= take
        out.append({"date": day, "count": len(items), "items": items[:take]})
    result = {"date_from": date_from, "date_to": date_to, "days": out}
    if not out:
        result["note"] = f"Nothing dated between {date_from} and {date_to}. " + coverage_line(coverage(wiki))
    if dropped:
        result["note"] = (f"{dropped} items were left out (limits: {per_day} per day, {max_total} in all; each day's count is complete); "
                          "raise per_day, or narrow by section, scope or date range.")
    return result


COMPACT_SESSIONS_PER_DAY = 30      # guards abuse; a real day has far fewer sessions


def _compact(wiki, date_from: str, date_to: str, days: dict[str, list[dict]]) -> dict:
    """The week-at-a-glance shape (ported from the personal brain, 2026-10-29): each day's sessions in full,
    everything else as counts by kind and page type, so a week fits in one reply with nothing cut."""
    out, extra_sessions = [], 0
    for day in sorted(days):
        items = days[day]
        sessions = sorted((i for i in items if i["what"] == "session"), key=lambda i: str(i["title"]).lower())
        extra_sessions += max(0, len(sessions) - COMPACT_SESSIONS_PER_DAY)
        counts: dict[str, dict[str, int]] = {}
        for i in items:
            if i["what"] != "session":
                bucket = counts.setdefault(i["what"], {})
                bucket[i["type"] or "page"] = bucket.get(i["type"] or "page", 0) + 1
        out.append({"date": day, "count": len(items),
                    "sessions": [{"path": i["path"], "title": i["title"], "summary": str(i["summary"] or "")[:160]}
                                 for i in sessions[:COMPACT_SESSIONS_PER_DAY]],
                    "counts": counts})
    result = {"date_from": date_from, "date_to": date_to, "compact": True, "days": out}
    notes = [] if out else [f"Nothing dated between {date_from} and {date_to}. " + coverage_line(coverage(wiki))]
    if extra_sessions:
        notes.append(f"{extra_sessions} sessions beyond {COMPACT_SESSIONS_PER_DAY} a day were left out.")
    notes.append("Counts are pages created or changed that day; ask for one day without compact to list them.")
    result["note"] = " ".join(notes)
    return result


def log_range(lines: list[str], since: str | None, until: str | None) -> list[str]:
    """Log entries (`## [YYYY-MM-DD] kind | page — summary`) inside [since, until]."""
    out = []
    for line in lines:
        m = ISO.search(line[:16])
        if m and in_range(m.group(1), since, until):
            out.append(line)
    return out
