"""Small helpers shared by the compiler and the wiki store."""

from __future__ import annotations

import json
import re


def wikilink(path: str, title: str | None) -> str:
    # str() so a non-string title cannot crash an index rebuild; one line (a newline in a title forged index entries);
    # brackets and pipes break wikilink aliases.
    safe = " ".join(str(title or path).split()).replace("|", "/").replace("[", "(").replace("]", ")")
    return f"[[{path}|{safe}]]"


def frontmatter(d: dict) -> str:
    """YAML frontmatter with JSON-encoded values (valid YAML flow style). Empty values are omitted."""
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in d.items() if v not in (None, "", [])] + ["---", ""]
    return "\n".join(lines)


def first_line(text: str, n: int = 140) -> str:
    """First prose line of a page body, for the index catalog."""
    for line in text.split("\n"):
        # Bounded and "[" -free, like lint.LINK: the unbounded form was quadratic on "[[a|[[a|[[a|...".
        s = re.sub(r"\[\[[^\[\]|]{0,200}\|([^\]\n]{0,300})\]\]", r"\1", line).strip().lstrip("-*> ").strip()
        if s and not s.startswith(("#", "<", "|", "---", "```", "[")):
            return s if len(s) <= n else s[: n - 1].rstrip() + "…"
    return ""
