"""Tasks and future needs as first-class wiki records (ported from the personal second brain engine, 2026-10-29).

A task is a page `tasks/<slug>` with frontmatter `type: "task"`, `status`, `priority`, `kind` (Personal, Business,
Research), `due` (YYYY-MM-DD) and relations `project` and `session`. A future need is `future-needs/<slug>` with
`type: "future_need"`, `horizon`, `status` and relations `project` and `origin_session`. Allowed values are fixed below; a direct write with an invented value is refused.

Project and session pages show their records live: the list is computed when the page is read, never stored, so it
cannot go stale. `reference/tasks` is the stored rollup (also visible in Obsidian), regenerated with the hubs.
"""

from __future__ import annotations

import re

TASKS, NEEDS = "tasks", "future-needs"
TASK_TYPE, NEED_TYPE = "task", "future_need"
HUB = "reference/tasks"
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


ALLOWED = {
    TASK_TYPE: {"status": ["Not started", "In progress", "On Hold", "Done"], "priority": ["High", "Med", "Low"],
                "kind": ["Personal", "Business", "Research"]},
    NEED_TYPE: {"horizon": ["Now", "Next", "Later", "Someday"], "status": ["Open", "Promoted to Task", "Dropped"]},
}
OPEN_TASK = [s for s in ALLOWED[TASK_TYPE]["status"] if s != "Done"]
PRIORITY_RANK = {p: i for i, p in enumerate(ALLOWED[TASK_TYPE]["priority"])}


class RecordError(ValueError):
    pass


def validate(fm: dict, strict: bool) -> tuple[dict, list[str]]:
    """Check select fields and `due`. strict: raise on any bad value. Otherwise drop bad values and report them."""
    allowed = ALLOWED.get(fm.get("type"))
    if allowed is None:
        return fm, []
    out, dropped = dict(fm), []
    for key, values in allowed.items():
        v = out.get(key)
        if v in (None, ""):
            out.pop(key, None)
            continue
        if v not in values:
            if strict:
                raise RecordError(f"{key} must be one of {values}, not {v!r}")
            dropped.append(f"{key}={v}")
            out.pop(key)
    due = out.get("due")
    if due not in (None, "") and not (isinstance(due, str) and DATE.match(due)):
        if strict:
            raise RecordError(f"due must be YYYY-MM-DD, not {due!r}")
        dropped.append(f"due={due}")
        out.pop("due")
    if fm.get("type") == TASK_TYPE and "status" not in out:
        out["status"] = "Not started"
    if fm.get("type") == NEED_TYPE and "status" not in out:
        out["status"] = "Open"
    return out, dropped


def _records(wiki, of_type: str):
    for p, name in wiki._all():
        m = wiki._meta(name)
        if m["fm"].get("type") == of_type:
            yield p, m


def linked(wiki, target: str, of_type: str, relations: tuple[str, ...]) -> list[tuple[str, dict]]:
    """Records of `of_type` whose relation (any of `relations`) points at `target`."""
    return [(p, m) for p, m in _records(wiki, of_type) if any(r in relations and t == target for r, t in m["relations"])]


def _task_line(p: str, m: dict) -> str:
    fm = m["fm"]
    bits = [str(fm.get("status") or "Not started")]
    if fm.get("priority"):
        bits.append(str(fm["priority"]))
    if fm.get("due"):
        bits.append(f"due {fm['due']}")
    return f"- [[{p}|{fm.get('title') or p}]] ({', '.join(bits)})"


def _need_line(p: str, m: dict) -> str:
    fm = m["fm"]
    return f"- [[{p}|{fm.get('title') or p}]] ({fm.get('horizon') or 'no horizon'}, {fm.get('status') or 'Open'})"


def _sort_tasks(items):
    return sorted(items, key=lambda x: (x[1]["fm"].get("status") == "Done", PRIORITY_RANK.get(x[1]["fm"].get("priority"), 9),
                                        str(x[1]["fm"].get("due") or "9999"), str(x[1]["fm"].get("title") or "").lower()))


def live_block(wiki, p: str, page_type: str) -> str:
    """The live task and future-need list appended when a project or session page is read."""
    if page_type == "project":
        tasks, needs = linked(wiki, p, TASK_TYPE, ("project",)), linked(wiki, p, NEED_TYPE, ("project",))
    elif page_type == "session":
        tasks, needs = linked(wiki, p, TASK_TYPE, ("session",)), linked(wiki, p, NEED_TYPE, ("origin_session",))
    else:
        return ""
    if not tasks and not needs:
        return ""
    open_t = [x for x in tasks if x[1]["fm"].get("status") != "Done"]
    out = ["", "---", "", f"## Tasks (live: {len(open_t)} open, {len(tasks) - len(open_t)} done)", ""]
    out += [_task_line(*x) for x in _sort_tasks(tasks)] or ["- none"]
    if needs:
        out += ["", f"## Future needs (live: {sum(1 for _, m in needs if m['fm'].get('status') == 'Open')} open)", ""]
        out += [_need_line(*x) for x in sorted(needs, key=lambda x: str(x[1]["fm"].get("title") or "").lower())]
    out += ["", "_Computed from task and future-need records when this page was read; change them with wiki_task_update._", ""]
    return "\n".join(out)


def hub(wiki) -> tuple[str, int]:
    """The stored rollup: open tasks by project, done per session, open future needs by horizon."""
    tasks = list(_records(wiki, TASK_TYPE))
    needs = list(_records(wiki, NEED_TYPE))
    titles = {p: wiki._meta(n)["fm"].get("title") for p, n in wiki._all()}
    by_project: dict[str, list] = {}
    for p, m in tasks:
        if m["fm"].get("status") != "Done":
            proj = next((t for r, t in m["relations"] if r == "project"), "(no project)")
            by_project.setdefault(proj, []).append((p, m))
    n_open = sum(len(v) for v in by_project.values())
    n_done = sum(1 for _, m in tasks if m["fm"].get("status") == "Done")
    body = ["# Tasks and future needs", "",
            (f"Generated from the task and future-need records (do not edit). {n_open} open tasks, {n_done} done, "
             f"{len(needs)} future needs. Back to [[reference/start-here|Start here]]."), ""]
    body += ["## Open tasks by project", ""]
    for proj in sorted(by_project, key=lambda q: (-len(by_project[q]), str(titles.get(q) or q).lower())):
        label = f"[[{proj}|{titles.get(proj) or proj}]]" if proj in titles else proj
        body += [f"### {label} ({len(by_project[proj])} open)", "", *[_task_line(*x) for x in _sort_tasks(by_project[proj])], ""]
    done_by_session: dict[str, list[int]] = {}
    for p, m in tasks:
        s = next((t for r, t in m["relations"] if r == "session"), None)
        if s:
            c = done_by_session.setdefault(s, [0, 0])
            c[1] += 1
            c[0] += m["fm"].get("status") == "Done"
    if done_by_session:
        body += ["## Done per session", ""]
        for s in sorted(done_by_session, reverse=True)[:60]:
            d, t = done_by_session[s]
            body.append(f"- [[{s}|{titles.get(s) or s}]]: {d} of {t} done")
        body.append("")
    open_needs = [(p, m) for p, m in needs if m["fm"].get("status") == "Open"]
    if open_needs:
        body += ["## Open future needs by horizon", ""]
        for h in ALLOWED[NEED_TYPE]["horizon"] + [None]:
            group = [(p, m) for p, m in open_needs if m["fm"].get("horizon") == h]
            if group:
                body += [f"### {h or 'No horizon'} ({len(group)})", "", *[_need_line(*x) for x in sorted(group, key=lambda x: str(x[1]['fm'].get('title') or '').lower())], ""]
    return "\n".join(body), n_open
