"""Build a silo folder from reviewed seed pages, through the wiki engine (validation, scrubber, index, hubs).

    python -m tools.seed --src examples/seed/shared --dest <empty folder>
    python -m tools.wiki push --wiki <folder> --bucket <bucket> --prefix silos/shared

`tools.wiki push` copies files as they are, so seeding goes through `Wiki.write` first: every page is checked the
same way a live write is, and index.md, log.md and reference/start-here come out generated, not hand-made.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mcp_server.wiki_store import Wiki


def build(src: Path, dest: Path) -> dict:
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"{dest} is not empty; seed into a fresh folder")
    dest.mkdir(parents=True, exist_ok=True)
    wiki = Wiki(dest)
    pages = sorted(p for p in src.rglob("*.md"))
    for f in pages:
        path = f.relative_to(src).with_suffix("").as_posix()
        wiki.write(path, f.read_text(encoding="utf-8"), "seed")
    return {"pages": len(pages), "lint": wiki.lint()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.seed", description=__doc__.splitlines()[0])
    ap.add_argument("--src", required=True)
    ap.add_argument("--dest", required=True)
    a = ap.parse_args(argv)
    print(json.dumps(build(Path(a.src), Path(a.dest)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
