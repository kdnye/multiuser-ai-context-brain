"""Drift check between this canonical engine repo and a fork that carries copies of the engine files.

    python -m tools.port_check --brain DIR [--upstream DIR] [--manifest FILE] [--json]
    python -m tools.port_check --brain DIR --init [--force]
    python -m tools.port_check --brain DIR --accept PATH --reason TEXT [--todo port|upstream|none] [--diverge]
    python -m tools.port_check --brain DIR --set-base COMMIT --reason TEXT

Model. The engine is canonical here; a fork lists, in `engine-sync.json` (version 1, kept in the fork), how each engine
file stands: `synced` (equals upstream at `base_commit`, the commit the fork was last ported through), `diverged`
(different from upstream; pinned by the sha256 of the fork's file, with a reason and a `reviewed` flag), `owned` (a
policy file the fork owns, never compared) or `absent` (the fork deliberately lacks it). Files are compared with CRLF
turned into LF. Upstream is read at its COMMITTED HEAD through git; a dirty working tree or index is ignored.

A diverged entry starts unreviewed (`--init` writes `reviewed: false`) and FAILs until a person looks at it and runs
`--accept`, which records `reviewed`, the upstream commit it was reviewed through and an optional `todo` (`port`: the
fork must adopt upstream's version; `upstream`: the fork is ahead and its fix must be contributed here). `--set-base`
refuses to move the base over an upstream change to a diverged file that was not reviewed through the new base.

Every path in (engine list at upstream HEAD) UNION (manifest keys) UNION (fork files matching an engine glob) gets one
verdict: PASS, FAIL, NOT COVERED or COULD NOT RUN. Nothing passes by default: the overall verdict is PASS only if no row
FAILs and at least one synced or diverged file was positively compared. Exit 0 = PASS, 1 = FAIL or NOT COVERED (or a
refused maintenance request), 2 = COULD NOT RUN (bad upstream, base commit behind or missing, manifest, brain dir...).

Fork files and upstream contents are data: read, hashed and compared, never executed; a symlink or junction that leaves
the brain dir is an error; manifest paths that are absolute, contain `..` or control characters are rejected.
Maintenance modes write the manifest atomically (temp file, then rename) and never write after a COULD NOT RUN.
Stdlib only, no network.
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
BOM = b"\xef\xbb\xbf"
HISTORY_KEEP = 20
SHALLOW_HINT = "; shallow clone? run git fetch --unshallow in the upstream checkout"


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


def debom(data: bytes) -> bytes:
    return data.removeprefix(BOM)


def check_path(path: str, where: str) -> str:
    parts = path.split("/")
    bad = (not path or path.startswith("/") or "\\" in path or WINDOWS_DRIVE.match(path) or {"..", ".", ""} & set(parts)
           or any(ord(c) < 0x20 or ord(c) == 0x7F for c in path))
    if bad:
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
        rc, _, _ = self._run("rev-parse", "--git-dir")
        if rc != 0:
            raise CouldNotRun("upstream is not a git repository")
        self.head = self.resolve("HEAD")
        self._trees: dict[str, set[str]] = {}
        self._blobs: dict[tuple[str, str], bytes] = {}

    def _run(self, *args: str) -> tuple[int, bytes, str]:
        cmd = ["git", "-c", "core.quotepath=off", "-C", str(self.root), *args]
        try:
            done = subprocess.run(cmd, capture_output=True, check=False)
        except OSError as exc:
            raise CouldNotRun(f"cannot run git: {exc}") from exc
        return done.returncode, done.stdout, done.stderr.decode("utf-8", "replace").strip()[:200]

    def _git(self, *args: str, what: str = "git failed") -> bytes:
        rc, out, err = self._run(*args)
        if rc != 0:
            raise CouldNotRun(f"{what}: {err}")
        return out

    def resolve(self, rev: str, is_base: bool = False) -> str:
        """Full commit id for HEAD or a hex id. Refuses names, and hex ids that are also a branch or tag name."""
        if rev != "HEAD":
            if not REV.fullmatch(rev):
                raise CouldNotRun(f"not a commit id: {rev!r}")
            for ns in ("heads", "tags"):
                if self._run("rev-parse", "--verify", "--quiet", f"refs/{ns}/{rev}")[0] == 0:
                    raise CouldNotRun(f"{rev!r} is also a branch or tag name; use the full 40-character commit id")
        rc, out, _ = self._run("rev-parse", "--verify", f"{rev}^{{commit}}")
        if rc != 0:
            raise CouldNotRun(f"commit {rev} not found in upstream (or ambiguous)" + (SHALLOW_HINT if is_base else ""))
        return out.decode().strip()

    def has_commit(self, rev: str) -> bool:
        return FULL_SHA.fullmatch(rev) is not None and self._run("cat-file", "-e", f"{rev}^{{commit}}")[0] == 0

    def is_ancestor(self, older: str, newer: str) -> bool:
        rc, _, err = self._run("merge-base", "--is-ancestor", older, newer)
        if rc > 1:
            raise CouldNotRun(f"cannot compare commits: {err}")
        return rc == 0

    def branch(self) -> str | None:
        rc, out, _ = self._run("symbolic-ref", "-q", "--short", "HEAD")
        return out.decode().strip() if rc == 0 else None

    def default_branches(self) -> set[str]:
        names = {"main", "master"}
        rc, out, _ = self._run("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
        if rc == 0 and "/" in out.decode():
            names.add(out.decode().strip().split("/", 1)[1])
        return names

    def tree(self, rev: str) -> set[str]:
        if rev not in self._trees:
            out = self._git("ls-tree", "-r", "-z", "--name-only", rev, what=f"cannot list {rev}")
            self._trees[rev] = {n for n in out.decode("utf-8", "surrogateescape").split("\0") if n}
        return self._trees[rev]

    def show(self, rev: str, path: str) -> bytes | None:
        """Normalised bytes of `path` at `rev`, or None when that commit has no such file."""
        if path not in self.tree(rev):
            return None
        if (rev, path) not in self._blobs:
            self._blobs[rev, path] = normalise(self._git("show", f"{rev}:{path}", what=f"cannot read {path} at {rev}"))
        return self._blobs[rev, path]

    def changed_lines(self, base: str, path: str) -> int | None:
        """Changed-line count, or None for a binary file."""
        out = self._git("diff", "--numstat", base, self.head, "--", path, what="git diff failed").decode()
        total = 0
        for line in out.splitlines():
            added, deleted = line.split("\t")[:2]
            if added == "-" or deleted == "-":
                return None
            total += int(added) + int(deleted)
        return total

    def renames(self, base: str) -> dict[str, str]:
        """old path -> new path for renames between base and HEAD."""
        out = self._git("diff", "-M", "--name-status", "-z", base, self.head, what="git diff failed")
        tokens = out.decode("utf-8", "surrogateescape").split("\0")
        found, i = {}, 0
        while i < len(tokens) and tokens[i]:
            status = tokens[i]
            if status[0] in "RC" and i + 2 < len(tokens):
                if status[0] == "R":
                    found[tokens[i + 1]] = tokens[i + 2]
                i += 3
            else:
                i += 2
        return found

    def engine_entries(self) -> list[str]:
        raw = self.show(self.head, ENGINE_LIST)
        if raw is None:
            raise CouldNotRun(f"upstream HEAD has no {ENGINE_LIST}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CouldNotRun(f"{ENGINE_LIST} is not valid UTF-8: {exc}") from exc
        entries = (line.split("#", 1)[0].strip() for line in text.splitlines())
        return [check_path(e, "engine list") for e in entries if e]

    def expand(self, entries: list[str]) -> list[str]:
        tree, found = self.tree(self.head), set()
        for entry in entries:
            rx = glob_regex(entry)
            hits = {p for p in tree if rx.fullmatch(p)}
            if not hits:
                raise CouldNotRun(f"engine list entry matches no file at upstream HEAD: {entry}")
            found |= hits
        return sorted(found)


# ---- the fork ------------------------------------------------------------------------------------------------------

class Fork:
    def __init__(self, root: Path) -> None:
        if not root.is_dir():
            raise CouldNotRun(f"brain directory not found: {root}")
        self.root = root.resolve()

    def _inside(self, path: str) -> Path:
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise CouldNotRun(f"{path} resolves outside the brain directory")
        return resolved

    def read(self, path: str) -> bytes | None:
        """Normalised bytes of a fork file, None if there is no such file. Links may not leave the brain dir."""
        try:
            resolved = self._inside(path)
            return normalise(resolved.read_bytes()) if resolved.is_file() else None
        except OSError as exc:
            raise CouldNotRun(f"cannot read {path}: {exc}") from exc

    def present(self, path: str) -> bool:
        """True for a file OR a directory at that path."""
        try:
            return self._inside(path).exists()
        except OSError as exc:
            raise CouldNotRun(f"cannot read {path}: {exc}") from exc

    def glob(self, pattern: str) -> set[str]:
        """Fork files matching an engine-list entry (`*` and `?` stay inside one segment)."""
        current = [""]
        for seg in pattern.split("/"):
            nxt = []
            for prefix in current:
                if "*" in seg or "?" in seg:
                    rx = glob_regex(seg)
                    try:
                        names = sorted(os.listdir(self.root / prefix if prefix else self.root))
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    except OSError as exc:
                        raise CouldNotRun(f"cannot list {prefix or '.'}: {exc}") from exc
                    nxt += [f"{prefix}/{n}".lstrip("/") for n in names if rx.fullmatch(n)]
                else:
                    nxt.append(f"{prefix}/{seg}".lstrip("/"))
            current = nxt
        return {p for p in current if (self.root / p).is_file()}


# ---- manifest ------------------------------------------------------------------------------------------------------

def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError) as exc:
        raise CouldNotRun(f"cannot read manifest {path}: {type(exc).__name__}: {str(exc)[:120]}") from exc
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
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
        if "reviewed" in entry and type(entry["reviewed"]) is not bool:
            raise CouldNotRun(f"manifest entry {name}: reviewed must be true or false")
        if entry.get("todo") not in (None, "port", "upstream"):
            raise CouldNotRun(f"manifest entry {name}: todo must be port or upstream")
        through = entry.get("reviewed_through")
        if through is not None and not (isinstance(through, str) and FULL_SHA.fullmatch(through)):
            raise CouldNotRun(f"manifest entry {name}: reviewed_through must be a 40-character commit id")
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

def judge(path: str, entry: dict | None, up: Upstream, fork: Fork, base: str, to: dict[str, str], frm: dict[str, str]):
    """Return (status, verdict, note) for one path. `to`/`frm` map upstream renames old->new and new->old."""
    if entry is None:
        hint = f" (possibly renamed from {frm[path]})" if path in frm else ""
        return "-", "FAIL", "UNCLASSIFIED: in the engine list but not in the manifest" + hint
    status = entry["status"]
    if status == "owned":
        return status, "NOT COVERED", entry["reason"]
    if path not in up.tree(up.head):
        return status, "FAIL", "removed upstream" + (f" (possibly renamed to {to[path]})" if path in to else "")
    if status == "absent":
        return (status, "FAIL", "expected absent") if fork.present(path) else (status, "PASS", "")
    mine, at_base, at_head = fork.read(path), up.show(base, path), up.show(up.head, path)
    moved = at_base != at_head
    if status == "diverged":
        if mine is None or sha(mine) != entry["sha256"]:
            return status, "FAIL", "unreviewed local change"
        if not entry.get("reviewed"):
            return status, "FAIL", "baseline divergence not reviewed"
        if entry.get("todo") == "port":
            return status, "FAIL", "TODO: port"
        if entry.get("todo") == "upstream":
            return status, "FAIL", "TODO: upstream this"
        return (status, "FAIL", "NEEDS PORT (diverged: review the upstream diff)") if moved else (status, "PASS", "")
    if mine is not None and mine == at_base:
        if not moved:
            return status, "PASS", ""
        lines = up.changed_lines(base, path)
        return status, "FAIL", f"NEEDS PORT ({'binary' if lines is None else f'{lines} changed lines'})"
    if mine is not None and moved and mine == at_head:
        return status, "PASS", "ported to HEAD; run --set-base"
    hint = " (differs only by a BOM?)" if mine is not None and at_base is not None and debom(mine) == debom(at_base) else ""
    return status, "FAIL", "synced file edited locally" + hint


def run_check(up: Upstream, fork: Fork, manifest: dict) -> dict:
    base = up.resolve(manifest["base_commit"], is_base=True)
    if not up.is_ancestor(base, up.head):
        raise CouldNotRun("upstream checkout is behind base_commit (update it, or check you are on the right branch)")
    files, entries = manifest["files"], up.engine_entries()
    engine = up.expand(entries)
    to = up.renames(base)
    frm = {new: old for old, new in to.items()}
    rows = []
    for path in sorted(set(engine) | set(files)):
        status, verdict, note = judge(path, files.get(path), up, fork, base, to, frm)
        rows.append({"path": path, "status": status, "verdict": verdict, "note": note})
    fork_only = set().union(*(fork.glob(e) for e in entries)) - up.tree(up.head) - set(files)
    rows += [{"path": p, "status": "-", "verdict": "NOT COVERED", "note": "fork-only"} for p in sorted(fork_only)]
    rows.sort(key=lambda r: r["path"])
    counts = {v: sum(r["verdict"] == v for r in rows) for v in ("PASS", "FAIL", "NOT COVERED")}
    compared = sum(r["verdict"] == "PASS" and r["status"] in ("synced", "diverged") for r in rows)
    verdict = "FAIL" if counts["FAIL"] else "PASS" if compared else "NOT COVERED"
    branch, warnings = up.branch(), []
    if branch is None:
        warnings.append("upstream checkout is on a detached HEAD; check it is the commit you mean")
    elif branch not in up.default_branches():
        warnings.append(f"upstream checkout is on branch {branch}, not the default branch")
    return {"verdict": verdict, "base_commit": base, "head_commit": up.head, "upstream_branch": branch or "(detached)",
            "warnings": warnings, "compared": compared, "counts": counts, "rows": rows}


def render(report: dict) -> str:
    rows = report["rows"]
    w = [max(len(h), *(len(r[k]) for r in rows)) for h, k in (("PATH", "path"), ("STATUS", "status"), ("VERDICT", "verdict"))]
    lines = [f"warning: {m}" for m in report["warnings"]]
    lines.append(f"{'PATH':<{w[0]}}  {'STATUS':<{w[1]}}  {'VERDICT':<{w[2]}}  NOTE")
    lines += [f"{r['path']:<{w[0]}}  {r['status']:<{w[1]}}  {r['verdict']:<{w[2]}}  {r['note']}".rstrip() for r in rows]
    c = report["counts"]
    lines.append(f"port check: {report['verdict']} - {c['PASS']} PASS, {c['FAIL']} FAIL, {c['NOT COVERED']} NOT COVERED, "
                 f"{report['compared']} compared (base {report['base_commit'][:10]}, upstream {report['upstream_branch']} "
                 f"{report['head_commit'][:10]})")
    return "\n".join(lines)


# ---- maintenance modes ---------------------------------------------------------------------------------------------

def do_init(up: Upstream, fork: Fork, manifest_path: Path, force: bool) -> str:
    existing, note = None, ""
    if manifest_path.exists():
        if not force:
            raise Refused(f"{manifest_path} already exists (use --force to add new engine files to it)")
        bak = manifest_path.with_name(manifest_path.name + ".bak")
        try:
            bak.write_bytes(manifest_path.read_bytes())
        except OSError as exc:
            raise CouldNotRun(f"cannot write backup {bak}: {exc}") from exc
        note = f"; old manifest kept as {bak.name}"
        try:
            existing = load_manifest(manifest_path)
        except CouldNotRun as exc:
            note += f" (it was unreadable, so nothing could be merged: {exc})"
    base = up.resolve(existing["base_commit"], is_base=True) if existing else up.head
    data = existing or {"version": 1, "upstream_note": "Canonical engine repo; see docs/PORTING.md.",
                        "base_commit": base, "synced_on": today(), "files": {}}
    files, added = data["files"], 0
    for path in up.expand(up.engine_entries()):
        if path in files:
            continue
        added += 1
        mine = fork.read(path)
        if mine is None:
            files[path] = {"status": "absent", "reason": "not present in this fork"}
        elif mine == up.show(base, path):
            files[path] = {"status": "synced"}
        else:
            files[path] = {"status": "diverged", "sha256": sha(mine), "reviewed": False,
                           "reason": f"baseline divergence recorded {today()}; replace with the real reason"}
    save_manifest(manifest_path, data)
    counts = {s: sum(f["status"] == s for f in files.values()) for s in STATUSES}
    return (f"init: wrote {manifest_path} at {base[:10]} ({added} new entries): "
            + ", ".join(f"{n} {s}" for s, n in counts.items()) + note)


def do_accept(up: Upstream, fork: Fork, manifest_path: Path, path: str, reason: str, todo: str | None, diverge: bool) -> str:
    data = load_manifest(manifest_path)
    check_path(path, "--accept")
    entry = data["files"].get(path)
    want = "synced" if diverge else "diverged"
    if entry is None or entry["status"] != want:
        raise Refused(f"{path} is not {want} in the manifest (status: {entry['status'] if entry else 'not listed'})")
    mine = fork.read(path)
    if mine is None:
        raise Refused(f"{path} does not exist in the fork")
    entry["status"] = "diverged"
    entry.update(sha256=sha(mine), reason=reason.strip(), reviewed=True, reviewed_through=up.head)
    if todo == "none":
        entry.pop("todo", None)
    elif todo:
        entry["todo"] = todo
    save_manifest(manifest_path, data)
    return f"accept: {path} pinned to {entry['sha256'][:12]}, reviewed through {up.head[:10]}"


def do_set_base(up: Upstream, fork: Fork, manifest_path: Path, commit: str, reason: str) -> str:
    data = load_manifest(manifest_path)
    current, target = up.resolve(data["base_commit"], is_base=True), up.resolve(commit)
    if not up.is_ancestor(current, target):
        raise Refused(f"{target[:10]} is not a descendant of the current base {current[:10]}")
    problems = []
    for path, entry in sorted(data["files"].items()):
        mine = fork.read(path)
        if entry["status"] == "synced" and (mine is None or mine != up.show(target, path)):
            problems.append(f"{path}: synced file does not equal upstream at {target[:10]}")
        if entry["status"] != "diverged":
            continue
        if mine is None or sha(mine) != entry["sha256"]:
            problems.append(f"{path}: diverged file changed since it was pinned (run --accept after reviewing it)")
        elif not entry.get("reviewed"):
            problems.append(f"{path}: baseline divergence not reviewed (run --accept)")
        elif up.show(current, path) != up.show(target, path):
            through = entry.get("reviewed_through")
            if not (through and up.has_commit(through) and up.is_ancestor(target, through)):
                problems.append(f"{path}: changed upstream since the base and not reviewed through {target[:10]} (port it, then --accept)")
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
    mode.add_argument("--accept", metavar="PATH", help="record a reviewed diverged file (needs --reason)")
    mode.add_argument("--set-base", metavar="COMMIT", help="record the fork as ported through COMMIT (needs --reason)")
    p.add_argument("--force", action="store_true", help="with --init: add new engine files to an existing manifest (keeps a .bak)")
    p.add_argument("--reason", help="why (for --accept and --set-base)")
    p.add_argument("--todo", choices=("port", "upstream", "none"), help="with --accept: mark follow-up work, or clear it")
    p.add_argument("--diverge", action="store_true", help="with --accept: turn a synced file into a diverged one")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    maintenance = bool(args.init or args.accept or args.set_base)
    if (args.todo or args.diverge) and not args.accept:
        print("port check: --todo and --diverge belong with --accept", file=sys.stderr)
        return 1
    if (args.accept or args.set_base) and not (args.reason and args.reason.strip()):
        print("port check: --reason is required with --accept and --set-base", file=sys.stderr)
        return 1
    try:
        fork = Fork(args.brain)
        manifest_path = args.manifest or fork.root / "engine-sync.json"
        up = Upstream(args.upstream)
        if args.accept:
            print(do_accept(up, fork, manifest_path, args.accept, args.reason, args.todo, args.diverge))
        elif args.init:
            print(do_init(up, fork, manifest_path, args.force))
        elif args.set_base:
            print(do_set_base(up, fork, manifest_path, args.set_base, args.reason))
        else:
            report = run_check(up, fork, load_manifest(manifest_path))
            print(json.dumps(report, indent=2) if args.json else render(report))
            return 0 if report["verdict"] == "PASS" else 1
        return 0
    except Refused as exc:
        print(f"port check: refused: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - any failure must be a reported COULD NOT RUN, never a traceback
        message = str(exc) if isinstance(exc, CouldNotRun) else f"unexpected {type(exc).__name__}: {str(exc)[:150]}"
        if args.json and not maintenance:
            print(json.dumps({"verdict": "COULD NOT RUN", "error": message}, indent=2))
        else:
            print(f"port check: COULD NOT RUN: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
