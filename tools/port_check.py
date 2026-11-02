"""Drift check between this canonical engine repo and a fork that carries copies of the engine files.

    python -m tools.port_check --brain DIR [--upstream DIR] [--manifest FILE] [--json]
    python -m tools.port_check --brain DIR --init [--force]
    python -m tools.port_check --brain DIR --accept PATH --reason TEXT
    python -m tools.port_check --brain DIR --set-base COMMIT --reason TEXT

Model. The engine is canonical here; a fork lists, in `engine-sync.json` (version 1, kept in the fork), how each engine
file stands: `synced` (equals upstream at `base_commit`, the commit the fork was last ported through), `diverged`
(deliberately different, pinned by the sha256 of the fork's file, with a reason), `owned` (a policy file the fork owns,
never compared) or `absent` (the fork deliberately lacks it). Files are compared with CRLF turned into LF.

Every path in (engine list at upstream HEAD) UNION (manifest keys) gets one verdict: PASS, FAIL, NOT COVERED or
COULD NOT RUN. Nothing passes by default: a file is PASS only after a positive comparison. Exit 0 = no FAIL, 1 = any
FAIL (or a refused maintenance request), 2 = COULD NOT RUN (bad upstream, base commit, manifest or brain dir).

Fork files and upstream contents are data: they are read, hashed and compared, never executed; a symlink that leaves
the brain dir is an error; manifest paths that are absolute or contain `..` are rejected. Maintenance modes write the
manifest atomically (temp file, then rename) and never write after a COULD NOT RUN. Stdlib only, no network.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_LIST = "tools/engine_files.txt"
STATUSES = ("synced", "diverged", "owned", "absent")
NEEDS_REASON = ("diverged", "owned", "absent")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
REV = re.compile(r"[0-9a-fA-F]{4,40}")
WINDOWS_DRIVE = re.compile(r"[A-Za-z]:")
HISTORY_KEEP = 20


class CouldNotRun(Exception):
    """The check cannot be trusted to answer (exit 2)."""


class Refused(Exception):
    """A maintenance request was refused; nothing was written (exit 1)."""


def normalise(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def sha(data: bytes) -> str:
    return hashlib.sha256(normalise(data)).hexdigest()


def today() -> str:
    return dt.date.today().isoformat()


def check_path(path: str, where: str) -> str:
    parts = path.split("/")
    if not path or path.startswith("/") or "\\" in path or WINDOWS_DRIVE.match(path) or {"..", ".", ""} & set(parts):
        raise CouldNotRun(f"unsafe path in {where}: {path!r}")
    return path


def glob_regex(pattern: str) -> re.Pattern[str]:
    """`*` and `?` stay inside one path segment; everything else is literal."""
    out = "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in pattern)
    return re.compile(out)


# ---- upstream (a git checkout, read through git only) -------------------------------------------------------------

class Upstream:
    def __init__(self, root: Path) -> None:
        self.root = root
        if not root.is_dir():
            raise CouldNotRun(f"upstream is not a directory: {root}")
        self._git("rev-parse", "--git-dir", what="upstream is not a git repository")
        self.head = self.resolve("HEAD")
        self._trees: dict[str, set[str]] = {}

    def _git(self, *args: str, what: str = "git failed") -> bytes:
        cmd = ["git", "-c", "core.quotepath=off", "-C", str(self.root), *args]
        try:
            done = subprocess.run(cmd, capture_output=True, check=False)
        except OSError as exc:
            raise CouldNotRun(f"cannot run git: {exc}") from exc
        if done.returncode != 0:
            raise CouldNotRun(f"{what}: {done.stderr.decode('utf-8', 'replace').strip()[:200]}")
        return done.stdout

    def resolve(self, rev: str) -> str:
        if rev != "HEAD" and not REV.fullmatch(rev):
            raise CouldNotRun(f"not a commit id: {rev!r}")
        out = self._git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", what=f"commit {rev} not found in upstream")
        return out.decode().strip()

    def tree(self, rev: str) -> set[str]:
        if rev not in self._trees:
            out = self._git("ls-tree", "-r", "-z", "--name-only", rev, what=f"cannot list {rev}")
            self._trees[rev] = {n for n in out.decode("utf-8", "surrogateescape").split("\0") if n}
        return self._trees[rev]

    def show(self, rev: str, path: str) -> bytes | None:
        """Normalised bytes of `path` at `rev`, or None when that commit has no such file."""
        if path not in self.tree(rev):
            return None
        return normalise(self._git("show", f"{rev}:{path}", what=f"cannot read {path} at {rev}"))

    def changed_lines(self, base: str, path: str) -> int:
        out = self._git("diff", "--numstat", base, self.head, "--", path, what="git diff failed").decode()
        total = 0
        for line in out.splitlines():
            added, deleted = line.split("\t")[:2]
            total += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
        return total

    def engine_files(self) -> list[str]:
        raw = self.show(self.head, ENGINE_LIST)
        if raw is None:
            raise CouldNotRun(f"upstream HEAD has no {ENGINE_LIST}")
        tree = self.tree(self.head)
        found: set[str] = set()
        for line in raw.decode("utf-8").splitlines():
            entry = line.split("#", 1)[0].strip()
            if entry:
                rx = glob_regex(check_path(entry, "engine list"))
                found.update(p for p in tree if rx.fullmatch(p))
        return sorted(found)


# ---- the fork ------------------------------------------------------------------------------------------------------

class Fork:
    def __init__(self, root: Path) -> None:
        if not root.is_dir():
            raise CouldNotRun(f"brain directory not found: {root}")
        self.root = root.resolve()

    def read(self, path: str) -> bytes | None:
        """Normalised bytes of a fork file, None if absent. A symlink may not leave the brain dir."""
        try:
            resolved = (self.root / path).resolve()
            if not resolved.is_relative_to(self.root):
                raise CouldNotRun(f"{path} resolves outside the brain directory")
            return normalise(resolved.read_bytes()) if resolved.is_file() else None
        except OSError as exc:
            raise CouldNotRun(f"cannot read {path}: {exc}") from exc


# ---- manifest ------------------------------------------------------------------------------------------------------

def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CouldNotRun(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise CouldNotRun("manifest must be a JSON object with version 1")
    if not isinstance(data.get("base_commit"), str) or not FULL_SHA.fullmatch(data["base_commit"]):
        raise CouldNotRun("manifest base_commit must be a 40-character lowercase hex commit id")
    files = data.get("files")
    if not isinstance(files, dict):
        raise CouldNotRun("manifest files must be an object")
    for name, entry in files.items():
        check_path(name, "manifest")
        if not isinstance(entry, dict) or entry.get("status") not in STATUSES:
            raise CouldNotRun(f"manifest entry {name}: status must be one of {', '.join(STATUSES)}")
        if entry["status"] in NEEDS_REASON and not (isinstance(entry.get("reason"), str) and entry["reason"].strip()):
            raise CouldNotRun(f"manifest entry {name}: a reason is required for {entry['status']}")
        if entry["status"] == "diverged" and not (isinstance(entry.get("sha256"), str) and SHA256.fullmatch(entry["sha256"])):
            raise CouldNotRun(f"manifest entry {name}: diverged needs a 64-character sha256")
    return data


def save_manifest(path: Path, data: dict) -> None:
    data["files"] = dict(sorted(data["files"].items()))
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".engine-sync-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
        raise CouldNotRun(f"cannot write manifest: {exc}") from exc


# ---- the check -----------------------------------------------------------------------------------------------------

def judge(path: str, entry: dict | None, up: Upstream, fork: Fork, base: str) -> tuple[str, str, str]:
    """Return (status, verdict, note) for one path."""
    if entry is None:
        return "-", "FAIL", "UNCLASSIFIED: in the engine list but not in the manifest"
    status = entry["status"]
    if path not in up.tree(up.head):
        return status, "FAIL", "removed upstream"
    if status == "owned":
        return status, "NOT COVERED", entry["reason"]
    mine = fork.read(path)
    if status == "absent":
        return (status, "FAIL", "expected absent") if mine is not None else (status, "PASS", "")
    moved = up.show(base, path) != up.show(up.head, path)
    if status == "diverged":
        if mine is None or sha(mine) != entry["sha256"]:
            return status, "FAIL", "unreviewed local change"
        return (status, "FAIL", "NEEDS PORT (diverged: review the upstream diff)") if moved else (status, "PASS", "")
    if mine is None or mine != up.show(base, path):
        return status, "FAIL", "synced file edited locally"
    return (status, "FAIL", f"NEEDS PORT ({up.changed_lines(base, path)} changed lines)") if moved else (status, "PASS", "")


def run_check(up: Upstream, fork: Fork, manifest: dict) -> dict:
    base = up.resolve(manifest["base_commit"])
    files = manifest["files"]
    engine = up.engine_files()
    if not engine:
        raise CouldNotRun("the engine list expands to no files")
    rows = []
    for path in sorted(set(engine) | set(files)):
        status, verdict, note = judge(path, files.get(path), up, fork, base)
        rows.append({"path": path, "status": status, "verdict": verdict, "note": note})
    counts = {v: sum(r["verdict"] == v for r in rows) for v in ("PASS", "FAIL", "NOT COVERED")}
    return {"verdict": "FAIL" if counts["FAIL"] else "PASS", "base_commit": base, "head_commit": up.head,
            "counts": counts, "rows": rows}


def render(report: dict) -> str:
    rows = report["rows"]
    w = [max(len(h), *(len(r[k]) for r in rows)) for h, k in (("PATH", "path"), ("STATUS", "status"), ("VERDICT", "verdict"))]
    lines = [f"{'PATH':<{w[0]}}  {'STATUS':<{w[1]}}  {'VERDICT':<{w[2]}}  NOTE"]
    lines += [f"{r['path']:<{w[0]}}  {r['status']:<{w[1]}}  {r['verdict']:<{w[2]}}  {r['note']}".rstrip() for r in rows]
    c = report["counts"]
    lines.append(f"port check: {report['verdict']} - {c['PASS']} PASS, {c['FAIL']} FAIL, {c['NOT COVERED']} NOT COVERED "
                 f"(base {report['base_commit'][:10]}, upstream HEAD {report['head_commit'][:10]})")
    return "\n".join(lines)


# ---- maintenance modes ---------------------------------------------------------------------------------------------

def do_init(up: Upstream, fork: Fork, manifest_path: Path, force: bool) -> str:
    if manifest_path.exists() and not force:
        raise Refused(f"{manifest_path} already exists (use --force to overwrite)")
    engine = up.engine_files()
    if not engine:
        raise CouldNotRun("the engine list expands to no files")
    files: dict[str, dict] = {}
    for path in engine:
        mine = fork.read(path)
        if mine is None:
            files[path] = {"status": "absent", "reason": "not present in this fork"}
        elif mine == up.show(up.head, path):
            files[path] = {"status": "synced"}
        else:
            files[path] = {"status": "diverged", "sha256": sha(mine),
                           "reason": f"baseline divergence recorded {today()}; replace with the real reason"}
    save_manifest(manifest_path, {"version": 1, "upstream_note": "Canonical engine repo; see docs/PORTING.md.",
                                  "base_commit": up.head, "synced_on": today(), "files": files})
    counts = {s: sum(f["status"] == s for f in files.values()) for s in STATUSES}
    return f"init: wrote {manifest_path} at {up.head[:10]}: " + ", ".join(f"{n} {s}" for s, n in counts.items())


def do_accept(fork: Fork, manifest_path: Path, path: str, reason: str) -> str:
    data = load_manifest(manifest_path)
    check_path(path, "--accept")
    entry = data["files"].get(path)
    if entry is None or entry["status"] != "diverged":
        raise Refused(f"{path} is not diverged in the manifest (status: {entry['status'] if entry else 'not listed'})")
    mine = fork.read(path)
    if mine is None:
        raise Refused(f"{path} does not exist in the fork")
    entry["sha256"], entry["reason"] = sha(mine), reason.strip()
    save_manifest(manifest_path, data)
    return f"accept: {path} re-pinned to {entry['sha256'][:12]}"


def do_set_base(up: Upstream, fork: Fork, manifest_path: Path, commit: str, reason: str) -> str:
    data = load_manifest(manifest_path)
    target = up.resolve(commit)
    problems = []
    for path, entry in sorted(data["files"].items()):
        mine = fork.read(path)
        if entry["status"] == "synced" and (mine is None or mine != up.show(target, path)):
            problems.append(f"{path}: synced file does not equal upstream at {target[:10]}")
        if entry["status"] == "diverged" and (mine is None or sha(mine) != entry["sha256"]):
            problems.append(f"{path}: diverged file changed since it was pinned (run --accept after reviewing it)")
    if problems:
        raise Refused("cannot move the base:\n  " + "\n  ".join(problems))
    data["base_commit"], data["synced_on"] = target, today()
    history = data["history"] if isinstance(data.get("history"), list) else []
    history.append({"date": today(), "commit": target, "reason": reason.strip()})
    data["history"] = history[-HISTORY_KEEP:]
    save_manifest(manifest_path, data)
    return f"set-base: base is now {target[:10]}"


# ---- CLI -----------------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.port_check", description="Show drift between this engine repo and a fork.")
    p.add_argument("--brain", required=True, type=Path, help="the fork's directory")
    p.add_argument("--upstream", type=Path, default=ROOT, help="the canonical engine checkout (default: this repo)")
    p.add_argument("--manifest", type=Path, help="default: <brain>/engine-sync.json")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write a first manifest from the fork's current files")
    mode.add_argument("--accept", metavar="PATH", help="re-pin a diverged file after reviewing it (needs --reason)")
    mode.add_argument("--set-base", metavar="COMMIT", help="record the fork as ported through COMMIT (needs --reason)")
    p.add_argument("--force", action="store_true", help="with --init: overwrite an existing manifest")
    p.add_argument("--reason", help="why (for --accept and --set-base)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    maintenance = bool(args.init or args.accept or args.set_base)
    if (args.accept or args.set_base) and not (args.reason and args.reason.strip()):
        print("port check: --reason is required with --accept and --set-base", file=sys.stderr)
        return 1
    try:
        fork = Fork(args.brain)
        manifest_path = args.manifest or fork.root / "engine-sync.json"
        if args.accept:
            print(do_accept(fork, manifest_path, args.accept, args.reason))
        elif args.init:
            print(do_init(Upstream(args.upstream), fork, manifest_path, args.force))
        elif args.set_base:
            print(do_set_base(Upstream(args.upstream), fork, manifest_path, args.set_base, args.reason))
        else:
            report = run_check(Upstream(args.upstream), fork, load_manifest(manifest_path))
            print(json.dumps(report, indent=2) if args.json else render(report))
            return 1 if report["counts"]["FAIL"] else 0
        return 0
    except Refused as exc:
        print(f"port check: refused: {exc}", file=sys.stderr)
        return 1
    except CouldNotRun as exc:
        if args.json and not maintenance:
            print(json.dumps({"verdict": "COULD NOT RUN", "error": str(exc)}, indent=2))
        else:
            print(f"port check: COULD NOT RUN: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
