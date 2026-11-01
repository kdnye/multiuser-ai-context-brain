"""The employee-facing wiki: Diataxis sections, app and dashboard cards, role portals, owners and review dates.

Layout (Diataxis is the standard split for staff documentation):
- `tutorials/`  learning by doing, start to finish (onboarding, "your first quote", training courses).
- `how-to/`     one task, numbered steps, for someone who already knows the basics (runbooks, SOPs).
- `reference/`  facts to look up (exists already: policies, contacts by function, glossaries, app READMEs).
- `explanation/` why things are the way they are (background, decisions, how the systems fit together).
- `apps/`       one card per internal app: what it is for, link, owner, training, health page.
- `dashboards/` one card per dashboard or saved report in a system of record: a LINK and who should use it, never the data.
- `roles/`      one portal per job function (ceo, cfo, accounting, office-manager, ...). When read, a portal lists
                every page whose `audience` includes it, computed live, so it never goes stale.

Every page in these sections carries `owner` (a people/<slug> path or a role name) and `review_by` (YYYY-MM-DD);
lint reports pages without them and pages past their review date. `audience` is a list of role slugs (each must be
an existing `roles/<slug>` page) or `["all"]`. Audience is about who a page is FOR; access is still decided by the
silo (access.py), so a finance dashboard card belongs in the `finance` silo, not in `shared` with a finance audience.
"""

from __future__ import annotations

import re

DIATAXIS = ("tutorials", "how-to", "reference", "explanation")
CARDS = ("apps", "dashboards")
ROLES = "roles"
SECTIONS = ("tutorials", "how-to", "explanation", "apps", "dashboards", "roles")   # new; `reference` already exists
OWNED = (*DIATAXIS, *CARDS, ROLES)
ORDER = (*CARDS, *DIATAXIS)            # how a role portal groups its pages
LABEL = {"apps": "Apps", "dashboards": "Dashboards", "tutorials": "Tutorials (learn)", "how-to": "How-to guides (do)",
         "reference": "Reference (look up)", "explanation": "Explanation (understand)"}
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")      # always fullmatch: `$` would accept a trailing newline
ALL = "all"


class EmployeeError(ValueError):
    pass


def section_of(p: str) -> str:
    return p.split("/", 1)[0]


def owned(p: str, fm: dict) -> bool:
    """Employee-facing pages that need an owner and a review date. Generated hubs under reference/ are exempt."""
    return section_of(p) in OWNED and fm.get("type") != "hub"


def audience(fm: dict) -> list[str]:
    a = fm.get("audience")
    return [a] if isinstance(a, str) else [str(x) for x in a or []]


def validate(p: str, fm: dict) -> None:
    """Refuse a malformed employee page on write. Missing owner or review date is a lint finding, not a refusal,
    so a draft can be saved; malformed values are refused because they would silently break portals and lint."""
    section = section_of(p)
    if section not in OWNED or fm.get("type") == "hub":
        return
    rb = fm.get("review_by")
    if rb not in (None, "") and not (isinstance(rb, str) and DATE.fullmatch(rb)):
        raise EmployeeError(f"review_by must be YYYY-MM-DD, not {rb!r}")
    a = fm.get("audience")
    if a is not None and not (isinstance(a, list) and all(isinstance(x, str) and x for x in a)):
        raise EmployeeError('audience must be a list of role slugs, e.g. ["accounting", "cfo"], or ["all"]')
    if section in CARDS:
        url = fm.get("url")
        if url and not str(url).startswith("https://"):
            raise EmployeeError("url must be an https:// link")
    if section == "dashboards":
        missing = [k for k in ("url", "system_role") if not fm.get(k)]
        if missing:
            raise EmployeeError(f"a dashboard card needs {', '.join(missing)}: link to the dashboard and name the system "
                                "role that can open it (the card never holds the data itself)")


def lint(wiki, today: str) -> list[dict]:
    """Owner, review date and audience findings for employee pages."""
    roles = {p.split("/", 1)[1] for p, _ in wiki._all() if p.startswith(ROLES + "/")}
    issues = []
    for p, name in wiki._all():
        fm = wiki._meta(name)["fm"]
        if not owned(p, fm):
            continue
        if not fm.get("owner"):
            issues.append({"kind": "missing_owner", "page": name})
        rb = fm.get("review_by")
        if not rb:
            issues.append({"kind": "missing_review_by", "page": name})
        elif not (isinstance(rb, str) and DATE.fullmatch(rb)):
            issues.append({"kind": "bad_review_by", "page": name, "review_by": str(rb)[:40]})
        elif rb < today:
            issues.append({"kind": "review_overdue", "page": name, "review_by": rb})
        for r in audience(fm):
            if r != ALL and r not in roles:
                issues.append({"kind": "unknown_audience", "page": name, "audience": r})
    return issues


def portal_block(wiki, p: str) -> str:
    """The live list appended when a role portal is read: every page for this role (or for all), by section."""
    role = p.split("/", 1)[1]
    groups: dict[str, list[tuple[str, dict, str]]] = {}
    for q, name in wiki._all():
        if q == p:
            continue
        m = wiki._meta(name)
        a = audience(m["fm"])
        if (role in a or ALL in a) and section_of(q) in ORDER:
            first = re.split(r"(?<=[.!?])\s", m["summary"] or "", maxsplit=1)[0]      # whole first sentence only
            groups.setdefault(section_of(q), []).append((q, m["fm"], first if first.endswith((".", "!", "?")) else ""))
    if not groups:
        return ""
    out = ["", "---", "", f"## Everything for this role (live: {sum(map(len, groups.values()))} pages)", ""]
    for s in ORDER:
        if s not in groups:
            continue
        out += [f"### {LABEL[s]}", ""]
        for q, fm, summary in sorted(groups[s], key=lambda x: str(x[1].get("title") or x[0]).lower()):
            link = f"[open]({fm['url']})" if s in CARDS and fm.get("url") else None
            owner = f"(owner: {fm['owner']})" if fm.get("owner") else ""
            tail = " ".join(b for b in (summary, owner) if b)
            out.append(f"- [[{q}|{fm.get('title') or q}]]" + (f" · {link}" if link else "") + (f": {tail}" if tail else ""))
        out.append("")
    out += ["_Computed from each page's `audience` when this portal was read; to add a page here, add this role to its audience._", ""]
    return "\n".join(out)


def start_block(wiki) -> list[str]:
    """The staff section of reference/start-here: role portals first, then the Diataxis sections."""
    pages = wiki._all()
    portals = sorted(((p, wiki._meta(n)["fm"]) for p, n in pages if p.startswith(ROLES + "/")),
                     key=lambda x: str(x[1].get("title") or x[0]).lower())
    counts = {s: sum(1 for p, _ in pages if p.startswith(s + "/")) for s in (*CARDS, *DIATAXIS)}
    if not portals and not any(counts[s] for s in (*CARDS, "tutorials", "how-to", "explanation")):
        return []
    out = ["## For staff", "", "Start from your role: its portal lists your apps, dashboards, trainings and guides.", ""]
    out += [f"- [[{p}|{fm.get('title') or p}]]" for p, fm in portals] or ["- (no role portals yet)"]
    out += ["", "Or by kind of document:", ""]
    out += [f"- {LABEL[s]}: {counts[s]} pages (`wiki_index(section=\"{s}\")`)" for s in ORDER if counts[s]]
    return [*out, ""]
