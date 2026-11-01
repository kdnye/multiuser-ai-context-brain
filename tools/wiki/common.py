"""Small helpers shared by the compiler and the wiki store."""

from __future__ import annotations

import json
import re


def wikilink(path: str, title: str | None) -> str:
    safe = (title or path).replace("|", "/").replace("[", "(").replace("]", ")")  # brackets and pipes break wikilink aliases
    return f"[[{path}|{safe}]]"


def frontmatter(d: dict) -> str:
    """YAML frontmatter with JSON-encoded values (valid YAML flow style). Empty values are omitted."""
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in d.items() if v not in (None, "", [])] + ["---", ""]
    return "\n".join(lines)


def first_line(text: str, n: int = 140) -> str:
    """First prose line of a page body, for the index catalog."""
    for line in text.splitlines():
        s = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", line).strip().lstrip("-*> ").strip()
        if s and not s.startswith(("#", "<", "|", "---", "```", "[")):
            return s if len(s) <= n else s[: n - 1].rstrip() + "…"
    return ""
