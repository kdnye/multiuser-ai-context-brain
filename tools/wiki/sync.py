"""Move the wiki between a folder and the cloud bucket: seed the bucket, and pull a copy for the physical mirror.

`push` seeds (or tops up) the bucket from the local wiki. By default it never overwrites a page that already
exists in the bucket, because after go-live the bucket is the source of truth and a local copy may be stale.
`pull` writes every page to a folder and a hash manifest next to it; the mirror box snapshots that folder
(restic or rsync), and the manifest lets a restore prove it is complete and unaltered.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mcp_server.store import Backend

MANIFEST = "MANIFEST.sha256.json"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def push(wiki: Path, backend: Backend, force: bool = False) -> dict:
    """Upload local pages. Returns counts of uploaded, skipped (already in the bucket) and unchanged pages."""
    existing = set(backend.list_names())
    counts = {"uploaded": 0, "skipped_existing": 0, "unchanged": 0}
    for f in sorted(wiki.rglob("*.md")):
        if ".git" in f.parts:
            continue
        name = f.relative_to(wiki).as_posix()
        text = f.read_text(encoding="utf-8")
        if name in existing:
            if backend.get(name) == text:
                counts["unchanged"] += 1
                continue
            if not force:
                counts["skipped_existing"] += 1
                continue
        backend.put(name, text)
        counts["uploaded"] += 1
    return counts


def pull(backend: Backend, dest: Path) -> dict:
    """Write every page in the bucket under `dest` and a manifest of their hashes. Returns counts and the manifest path."""
    dest.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for name in backend.list_names():
        text = backend.get(name)
        if text is None:  # deleted between list and get: skip it, the next pull will agree
            continue
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        manifest[name] = _sha(text)
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=0, sort_keys=True), encoding="utf-8", newline="\n")
    return {"pages": len(manifest), "manifest": str(dest / MANIFEST)}


def verify(dest: Path) -> dict:
    """Check a pulled folder against its manifest: missing, altered and extra pages."""
    manifest = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    missing, altered = [], []
    for name, digest in manifest.items():
        f = dest / name
        if not f.is_file():
            missing.append(name)
        elif _sha(f.read_text(encoding="utf-8")) != digest:
            altered.append(name)
    extra = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*.md") if p.relative_to(dest).as_posix() not in manifest)
    return {"pages": len(manifest), "missing": missing, "altered": altered, "extra": extra, "ok": not (missing or altered)}


def vault(backend: Backend, dest: Path) -> dict:
    """Mirror the bucket into an Obsidian vault folder, in place.

    Pulls into a staging folder and verifies it first; only a verified copy touches the vault. Then writes pages
    that are new or changed, removes .md pages that no longer exist in the bucket, and never touches anything
    under a dot-folder (Obsidian keeps its settings, plugins and workspace in `.obsidian/`). The vault is a
    read-only view: edits made in Obsidian are overwritten on the next run.
    """
    import shutil
    import tempfile

    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vault-stage-") as tmp:
        stage = Path(tmp)
        pull(backend, stage)
        check = verify(stage)
        if not check["ok"]:
            raise RuntimeError(f"staged copy failed verification: {check}")
        manifest = json.loads((stage / MANIFEST).read_text(encoding="utf-8"))
        written = unchanged = 0
        for name in manifest:
            src, dst = stage / name, dest / name
            text = src.read_text(encoding="utf-8")
            if dst.is_file() and dst.read_text(encoding="utf-8") == text:
                unchanged += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8", newline="\n")
            written += 1
        removed = []
        for f in dest.rglob("*.md"):
            rel = f.relative_to(dest)
            if any(part.startswith(".") for part in rel.parts):
                continue
            if rel.as_posix() not in manifest:
                f.unlink()
                removed.append(rel.as_posix())
        shutil.copyfile(stage / MANIFEST, dest / MANIFEST)
    for d in sorted((d for d in dest.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
        rel = d.relative_to(dest)
        if not any(part.startswith(".") for part in rel.parts) and not any(d.iterdir()):
            d.rmdir()
    return {"pages": len(manifest), "written": written, "unchanged": unchanged, "removed": sorted(removed)}
