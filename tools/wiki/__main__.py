"""Operator CLI for the context brain: move a silo between a folder and the bucket, verify copies, lint.

    python -m tools.wiki push   --wiki <folder> --bucket <bucket> --prefix silos/<silo> [--force]
    python -m tools.wiki pull   --bucket <bucket> --prefix silos/<silo> --dest <folder>
    python -m tools.wiki verify --dest <folder>
    python -m tools.wiki vault  --bucket <bucket> --prefix silos/<silo> --dest <obsidian-vault>
    python -m tools.wiki lint   --wiki <folder>

Operators work on one silo at a time; there is no command that reads across silos.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.wiki.sync import pull, push, vault, verify


def main(argv: list[str] | None = None, backend_factory=None) -> int:
    ap = argparse.ArgumentParser(prog="tools.wiki")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("push", help="seed a silo in the bucket from a folder (never overwrites unless --force)")
    ps.add_argument("--wiki", required=True)
    ps.add_argument("--bucket", required=True)
    ps.add_argument("--prefix", required=True)
    ps.add_argument("--force", action="store_true")
    for name, helptext in (("pull", "copy one silo to a folder with a hash manifest"), ("vault", "mirror one silo into an Obsidian vault")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--bucket", required=True)
        p.add_argument("--prefix", required=True)
        p.add_argument("--dest", required=True)
        if name == "vault":
            p.add_argument("--dry-run", action="store_true", help="report what would be written and removed, change nothing")
            p.add_argument("--force", action="store_true", help="mirror even if the destination looks unrelated to the bucket")
    vf =sub.add_parser("verify", help="check a pulled folder against its manifest")
    vf.add_argument("--dest", required=True)
    lt = sub.add_parser("lint", help="health checks on a silo folder (links, orphans, frontmatter, owners, review dates)")
    lt.add_argument("--wiki", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "verify":
        r = verify(Path(args.dest))
        print(json.dumps(r, indent=1))
        return 0 if r["ok"] else 1
    if args.cmd == "lint":
        # The full engine lint (orphans with the start-here and audience exemptions, plus the employee checks:
        # missing_owner, missing_review_by, review_overdue, unknown_audience), on a read-only view of the folder.
        from mcp_server.store import CachedStore
        from mcp_server.wiki_store import Wiki
        from tools.eval_recall import _ReadOnlyDir

        r = Wiki(CachedStore(_ReadOnlyDir(args.wiki))).lint()
        print(json.dumps(r, indent=1, ensure_ascii=False))
        return 1 if r["by_kind"] else 0
    from mcp_server.store import GcsBackend

    backend = backend_factory(args.bucket, args.prefix) if backend_factory else GcsBackend(args.bucket, args.prefix)
    if args.cmd == "push":
        r = push(Path(args.wiki), backend, force=args.force)
    elif args.cmd == "pull":
        r = pull(backend, Path(args.dest))
    else:
        try:
            r = vault(backend, Path(args.dest), dry_run=args.dry_run, force=args.force)
        except RuntimeError as exc:
            print(f"vault refused: {exc}", file=sys.stderr)
            return 2
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
