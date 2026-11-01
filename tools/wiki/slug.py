"""Stable, readable, unique page slugs."""

from __future__ import annotations

import re
import unicodedata


def slugify(text: str, max_len: int = 60) -> str:
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0] or slug[:max_len]
    return slug or "untitled"


def assign_slugs(items: list[tuple[str, str]], prefix: str = "") -> dict[str, str]:
    """{key: slug} for (key, title) pairs. A repeated slug gets the last 4 characters of its key."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for key, title in sorted(items, key=lambda kv: kv[0]):
        slug = prefix + slugify(title)
        if slug in taken:
            slug = f"{slug}-{key[-4:]}"
        n = 2
        while slug in taken:
            slug = f"{slug}-{n}"
            n += 1
        taken.add(slug)
        out[key] = slug
    return out
