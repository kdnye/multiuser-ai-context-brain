"""Health checks for the wiki, following the lint operation in Karpathy's LLM Wiki pattern.

Mechanical checks only; the LLM adds judgment (contradictions, stale claims, missing concepts) when it
runs the /lint workflow from CLAUDE.md.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# A backslash before | is the Obsidian escape inside a markdown table ([[path\|Title]]); it is not part of the path.
# Every group is bounded and cannot contain "[" or a newline: with unbounded groups an unclosed "[[a[[a[[a..." made each
# "[[" scan to the end of the text and backtrack, so a 12 KB page cost seconds and a 200 KB page minutes (quadratic).
LINK = re.compile(r"\[\[([^\[\]|#\\\n]{1,200})(?:\\?#[^\]|\n]{0,200})?(?:\\?\|[^\]\n]{0,300})?\]\]")
REQUIRED = ("title", "type", "scope", "source_ids", "updated")
SCOPES = {"personal", "work", "unscoped"}
SPECIAL = {"index.md", "log.md"}
# Typed relations in frontmatter (a page path or a list of them). They are links like [[...]], with a meaning.
RELATIONS = ("project", "parent", "related", "supersedes", "superseded_by", "depends_on", "people", "reports_to",
             "session", "origin_session")


def relation_targets(fm: dict) -> list[tuple[str, str]]:
    """(relation, target path) pairs from a page's frontmatter; values may be one path or a list."""
    out = []
    for key in RELATIONS:
        val = fm.get(key)
        for t in ([val] if isinstance(val, str) else val if isinstance(val, list) else []):
            if isinstance(t, str) and t.strip():
                out.append((key, t.strip().removesuffix(".md")))
    return out


def parse_frontmatter(text: str) -> dict:
    m = FRONTMATTER.match(text)
    out: dict = {}
    if m:
        # Split on "\n" only: str.splitlines() also splits on U+2028, U+2029 and U+0085, and a title containing one
        # (the JSON escape is stored raw) turned into extra frontmatter keys.
        for line in m.group(1).split("\n"):
            key, _, raw = line.partition(": ")
            try:
                out[key.strip()] = json.loads(raw)
            except ValueError:
                out[key.strip()] = raw.strip()
    return out


def lint(wiki: Path) -> dict:
    """Lint a wiki folder."""
    texts = {p.relative_to(wiki).as_posix(): p.read_text(encoding="utf-8") for p in wiki.rglob("*.md") if ".git" not in p.parts}
    return lint_pages(texts)


def lint_pages(texts: dict[str, str]) -> dict:
    """Lint pages given as {relative path: text}, wherever they are stored."""
    known = {rel[:-3] for rel in texts}
    issues: list[dict] = []
    inbound: dict[str, set] = defaultdict(set)
    titles: dict[tuple, list[str]] = defaultdict(list)
    index_targets = {m.group(1).strip() for m in LINK.finditer(texts.get("index.md", ""))}

    for rel, text in texts.items():
        fm = parse_frontmatter(text)
        body = FRONTMATTER.sub("", text, count=1)
        for target in (m.group(1).strip() for m in LINK.finditer(body)):
            if target not in known:
                issues.append({"kind": "broken_link", "page": rel, "target": target})
            elif rel not in SPECIAL:
                inbound[target].add(rel)
        for key, target in relation_targets(fm):
            if target not in known:
                issues.append({"kind": "broken_relation", "page": rel, "relation": key, "target": target})
            elif rel not in SPECIAL:
                inbound[target].add(rel)
        if rel in SPECIAL:
            continue
        for key in REQUIRED:
            if key not in fm:
                issues.append({"kind": "missing_frontmatter", "page": rel, "field": key})
        if fm.get("scope") not in SCOPES:
            issues.append({"kind": "bad_scope", "page": rel, "value": fm.get("scope")})
        elif fm["scope"] == "unscoped":
            issues.append({"kind": "unscoped", "page": rel})
        if rel[:-3] not in index_targets:
            issues.append({"kind": "not_in_index", "page": rel})
        if len(re.sub(r"^# .*$", "", body, flags=re.MULTILINE).strip()) < 20:
            issues.append({"kind": "empty_page", "page": rel})
        titles[(fm.get("type"), str(fm.get("title", "")).strip().lower())].append(rel)
    for rel in texts:
        if rel not in SPECIAL and not inbound[rel[:-3]]:
            issues.append({"kind": "orphan", "page": rel})
    for (kind, title), pages in titles.items():
        if len(pages) > 1 and title:
            issues.append({"kind": "duplicate_title", "type": kind, "title": title, "pages": pages})
    return {"pages": len(texts), "issues": issues, "by_kind": dict(Counter(i["kind"] for i in issues))}
