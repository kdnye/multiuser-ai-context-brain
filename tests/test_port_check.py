"""Tests for tools/port_check.py. Upstream and fork are temporary directories built here; no network."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools import port_check as pc

ROOT = Path(__file__).resolve().parents[1]
IDENT = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
FILES = ("a.py", "b.py", "pkg/c.py", "pkg/d.py")


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *IDENT, "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def put(root: Path, rel: str, text: str, newline: str = "\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", newline).encode())


def commit(repo: Path, msg: str = "c") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path):
    """upstream with a.py, b.py, pkg/c.py, pkg/d.py and an engine list; a fork that equals it at base."""
    up, fork = tmp_path / "up", tmp_path / "fork"
    up.mkdir()
    fork.mkdir()
    git(up, "init", "-q")
    put(up, "tools/engine_files.txt", "# engine\na.py\nb.py\npkg/*.py\n")
    for rel in FILES:
        put(up, rel, f"print('{rel}')\nline2\n")
        put(fork, rel, f"print('{rel}')\nline2\n")
    return up, fork, commit(up, "base")


def div(text: str, reason: str = "fork identity", **extra) -> dict:
    return {"status": "diverged", "sha256": pc.sha(text.encode()), "reason": reason, "reviewed": True, **extra}


def synced_manifest(fork: Path, base: str, **overrides) -> Path:
    files = {p: {"status": "synced"} for p in FILES}
    files.update(overrides)
    path = fork / "engine-sync.json"
    path.write_text(json.dumps({"version": 1, "base_commit": base, "files": files}), encoding="utf-8")
    return path


def run(up: Path, fork: Path, *extra: str):
    return pc.main(["--brain", str(fork), "--upstream", str(up), *extra])


def report(up: Path, fork: Path, capsys):
    capsys.readouterr()
    code = run(up, fork, "--json")
    return code, json.loads(capsys.readouterr().out)


def rows(rep):
    return {r["path"]: r for r in rep["rows"]}


def err(capsys) -> str:
    return capsys.readouterr().err


# ---- engine list ---------------------------------------------------------------------------------------------------

def test_engine_list_expands_to_existing_files():
    listed = []
    for line in (ROOT / "tools" / "engine_files.txt").read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            listed.append(entry)
            assert list(ROOT.glob(entry)), f"engine_files.txt entry matches nothing: {entry}"
    for wanted in ("mcp_server/__init__.py", "tools/__init__.py", "requirements-mcp.txt"):
        assert wanted in listed
    assert "tools/port_check.py" not in listed and "tools/release_denylist.sha256" not in listed


def test_glob_does_not_cross_directories(world):
    up, _, _ = world
    put(up, "pkg/sub/e.py", "x\n")
    commit(up)
    probe = pc.Upstream(up)
    assert probe.expand(probe.engine_entries()) == ["a.py", "b.py", "pkg/c.py", "pkg/d.py"]


def test_dead_glob_could_not_run_names_it(world, capsys):
    up, fork, base = world
    put(up, "tools/engine_files.txt", "a.py\nnothing/*.py\n")
    commit(up)
    synced_manifest(fork, base)
    assert run(up, fork) == 2
    assert "nothing/*.py" in err(capsys)


def test_non_utf8_engine_list_exits_2_without_traceback(world, capsys):
    up, fork, base = world
    (up / "tools" / "engine_files.txt").write_bytes(b"a.py\n\xff\xfe\n")
    commit(up)
    synced_manifest(fork, base)
    assert run(up, fork) == 2
    assert "UTF-8" in err(capsys)


# ---- verdicts ------------------------------------------------------------------------------------------------------

def test_all_synced_passes_and_exits_zero(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    code, rep = report(up, fork, capsys)
    assert code == 0 and rep["verdict"] == "PASS" and rep["compared"] == 4
    assert {r["verdict"] for r in rep["rows"]} == {"PASS"}


def test_json_shape(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    _, rep = report(up, fork, capsys)
    assert set(rep) == {"verdict", "base_commit", "head_commit", "upstream_branch", "warnings", "compared", "counts", "rows"}
    assert set(rep["counts"]) == {"PASS", "FAIL", "NOT COVERED"}
    assert set(rep["rows"][0]) == {"path", "status", "verdict", "note"}
    assert rep["base_commit"] == base and rep["warnings"] == []


def test_table_output(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    assert run(up, fork) == 0
    out = capsys.readouterr().out
    assert "PATH" in out and "a.py" in out and "port check: PASS - 4 PASS, 0 FAIL" in out


def test_crlf_in_fork_equals_lf_upstream(world, capsys):
    up, fork, base = world
    put(fork, "a.py", "print('a.py')\nline2\n", newline="\r\n")
    synced_manifest(fork, base)
    code, rep = report(up, fork, capsys)
    assert code == 0 and rows(rep)["a.py"]["verdict"] == "PASS"


def test_unclassified_file_fails(world, capsys):
    up, fork, base = world
    path = synced_manifest(fork, base)
    data = json.loads(path.read_text())
    del data["files"]["b.py"]
    path.write_text(json.dumps(data))
    code, rep = report(up, fork, capsys)
    assert code == 1 and "UNCLASSIFIED" in rows(rep)["b.py"]["note"]


def test_engine_list_growing_after_base_is_unclassified(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    put(up, "n.py", "new\n")
    put(up, "tools/engine_files.txt", "a.py\nb.py\npkg/*.py\nn.py\n")
    commit(up)
    code, rep = report(up, fork, capsys)
    assert code == 1 and "UNCLASSIFIED" in rows(rep)["n.py"]["note"]


def test_synced_file_edited_locally_fails(world, capsys):
    up, fork, base = world
    put(fork, "a.py", "changed\n")
    synced_manifest(fork, base)
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["a.py"]["note"] == "synced file edited locally"


def test_synced_file_missing_in_fork_fails(world, capsys):
    up, fork, base = world
    (fork / "a.py").unlink()
    synced_manifest(fork, base)
    _, rep = report(up, fork, capsys)
    assert rows(rep)["a.py"]["note"] == "synced file edited locally"


def test_needs_port_with_changed_line_count(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    put(up, "a.py", "print('a.py')\nline2\nline3\nline4\n")
    commit(up, "more")
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["a.py"]["note"] == "NEEDS PORT (2 changed lines)"
    assert rows(rep)["b.py"]["verdict"] == "PASS"


def test_ported_to_head_is_a_pass_with_note_and_keeps_other_fails(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    put(up, "a.py", "fixed\n")
    put(up, "b.py", "fixed b\n")
    new = commit(up, "fixes")
    put(fork, "a.py", "fixed\n")  # a correct port of a.py only
    code, rep = report(up, fork, capsys)
    assert rows(rep)["a.py"]["verdict"] == "PASS" and rows(rep)["a.py"]["note"] == "ported to HEAD; run --set-base"
    assert rows(rep)["b.py"]["verdict"] == "FAIL" and code == 1
    put(fork, "b.py", "fixed b\n")
    assert run(up, fork) == 0
    capsys.readouterr()
    assert run(up, fork, "--set-base", new, "--reason", "ported") == 0


def test_diverged_states(world, capsys):
    up, fork, base = world
    put(fork, "a.py", "local\n")
    manifest = synced_manifest(fork, base, **{"a.py": div("local\n")})
    code, rep = report(up, fork, capsys)
    assert code == 0 and rows(rep)["a.py"]["verdict"] == "PASS"
    put(fork, "a.py", "local, edited\n")
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["a.py"]["note"] == "unreviewed local change"
    data = json.loads(manifest.read_text())
    data["files"]["a.py"] = div("local, edited\n", reviewed=False)
    manifest.write_text(json.dumps(data))
    _, rep = report(up, fork, capsys)
    assert rows(rep)["a.py"]["note"] == "baseline divergence not reviewed"


def test_diverged_with_upstream_change_needs_port(world, capsys):
    up, fork, base = world
    put(fork, "a.py", "local\n")
    synced_manifest(fork, base, **{"a.py": div("local\n")})
    put(up, "a.py", "new upstream\n")
    commit(up)
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["a.py"]["note"].startswith("NEEDS PORT (diverged")


def test_owned_is_not_covered_and_never_compared(world, capsys):
    up, fork, base = world
    put(fork, "a.py", "anything at all\n")
    synced_manifest(fork, base, **{"a.py": {"status": "owned", "reason": "identity gate"}})
    code, rep = report(up, fork, capsys)
    assert code == 0 and rows(rep)["a.py"]["verdict"] == "NOT COVERED" and rows(rep)["a.py"]["note"] == "identity gate"


def test_all_owned_never_passes(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base, **{p: {"status": "owned", "reason": "mine"} for p in FILES})
    code, rep = report(up, fork, capsys)
    assert code == 1 and rep["verdict"] == "NOT COVERED" and rep["compared"] == 0


def test_absent_pass_fail_and_directory(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base, **{"a.py": {"status": "absent", "reason": "not used"}})
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["a.py"]["note"] == "expected absent"
    (fork / "a.py").unlink()
    code, rep = report(up, fork, capsys)
    assert code == 0 and rows(rep)["a.py"]["verdict"] == "PASS"
    (fork / "a.py").mkdir()
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["a.py"]["note"] == "expected absent"


def test_removed_upstream_fails(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    git(up, "rm", "-q", "b.py")
    put(up, "tools/engine_files.txt", "a.py\npkg/*.py\n")
    commit(up, "drop b")
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["b.py"]["note"] == "removed upstream"


def test_upstream_rename_is_noted_on_both_rows(world, capsys):
    up, fork, _ = world
    body = "".join(f"distinct line number {i}\n" for i in range(30))
    put(up, "b.py", body)
    put(fork, "b.py", body)
    base = commit(up, "long b")
    synced_manifest(fork, base)
    git(up, "mv", "b.py", "bb.py")
    put(up, "tools/engine_files.txt", "a.py\nbb.py\npkg/*.py\n")
    commit(up, "rename")
    code, rep = report(up, fork, capsys)
    assert code == 1
    assert "possibly renamed to bb.py" in rows(rep)["b.py"]["note"]
    assert "possibly renamed from b.py" in rows(rep)["bb.py"]["note"]


def test_manifest_key_outside_engine_list_is_reported(world, capsys):
    up, fork, _ = world
    put(up, "extra.py", "x\n")
    put(fork, "extra.py", "x\n")
    base = commit(up, "extra")
    synced_manifest(fork, base, **{"extra.py": {"status": "synced"}})
    code, rep = report(up, fork, capsys)
    assert code == 0 and "extra.py" in rows(rep)


def test_fork_only_owned_file_and_unlisted_fork_only_file(world, capsys):
    up, fork, base = world
    put(fork, "pkg/mine.py", "x\n")
    put(fork, "pkg/other.py", "y\n")
    synced_manifest(fork, base, **{"pkg/mine.py": {"status": "owned", "reason": "fork identity gate"}})
    code, rep = report(up, fork, capsys)
    assert code == 0
    assert rows(rep)["pkg/mine.py"]["verdict"] == "NOT COVERED" and rows(rep)["pkg/mine.py"]["note"] == "fork identity gate"
    assert rows(rep)["pkg/other.py"] == {"path": "pkg/other.py", "status": "-", "verdict": "NOT COVERED", "note": "fork-only"}


def test_binary_empty_and_bom_files(world, capsys):
    up, fork, _ = world
    for root in (up, fork):
        (root / "bin.dat").write_bytes(b"\x00\x01\x02\r\n\x03")
        (root / "empty.py").write_bytes(b"")
    put(up, "tools/engine_files.txt", "a.py\nb.py\npkg/*.py\nbin.dat\nempty.py\n")
    base = commit(up, "binary and empty")
    synced_manifest(fork, base, **{"bin.dat": {"status": "synced"}, "empty.py": {"status": "synced"}})
    code, rep = report(up, fork, capsys)
    assert code == 0 and rows(rep)["empty.py"]["verdict"] == "PASS" and rows(rep)["bin.dat"]["verdict"] == "PASS"
    (up / "bin.dat").write_bytes(b"\x00\x09\x09\x09")
    commit(up, "binary change")
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["bin.dat"]["note"] == "NEEDS PORT (binary)"
    (fork / "a.py").write_bytes(pc.BOM + b"print('a.py')\nline2\n")
    _, rep = report(up, fork, capsys)
    assert "BOM" in rows(rep)["a.py"]["note"]


def test_manifest_with_bom_is_read(world):
    up, fork, base = world
    manifest = synced_manifest(fork, base)
    manifest.write_bytes(pc.BOM + manifest.read_bytes())
    assert run(up, fork) == 0


def test_dirty_upstream_tree_is_ignored(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    (up / "a.py").write_text("uncommitted edit\n")
    (up / "tools" / "engine_files.txt").write_text("nothing/*.py\n")
    code, rep = report(up, fork, capsys)
    assert code == 0 and rep["head_commit"] == base


# ---- upstream state ------------------------------------------------------------------------------------------------

def test_upstream_behind_base_could_not_run(world, capsys):
    up, fork, _ = world
    put(up, "a.py", "newer\n")
    newer = commit(up, "newer")
    synced_manifest(fork, newer)
    git(up, "reset", "-q", "--hard", "HEAD~1")
    assert run(up, fork) == 2
    assert "behind base_commit" in err(capsys)


def test_branch_warnings(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    assert report(up, fork, capsys)[1]["warnings"] == []
    git(up, "checkout", "-q", "-b", "feature/x")
    _, rep = report(up, fork, capsys)
    assert rep["upstream_branch"] == "feature/x" and "not the default branch" in rep["warnings"][0]
    git(up, "checkout", "-q", "--detach")
    _, rep = report(up, fork, capsys)
    assert rep["upstream_branch"] == "(detached)" and "detached" in rep["warnings"][0]


def test_could_not_run_cases(world, tmp_path, capsys):
    up, fork, base = world
    manifest = synced_manifest(fork, base)
    assert run(tmp_path / "missing", fork) == 2
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run(plain, fork) == 2
    assert pc.main(["--brain", str(tmp_path / "nope"), "--upstream", str(up)]) == 2
    bad = json.loads(manifest.read_text())
    bad["base_commit"] = "0" * 40
    manifest.write_text(json.dumps(bad))
    capsys.readouterr()
    assert run(up, fork) == 2
    assert err(capsys).strip().endswith("run git fetch --unshallow in the upstream checkout")
    assert run(up, fork, "--json") == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "COULD NOT RUN"


# ---- manifest validation -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "{not json",
    "[]",
    "[" * 100000 + "]" * 100000,
    '{"version": 2, "base_commit": "%s", "files": {}}',
    '{"version": true, "base_commit": "%s", "files": {}}',
    '{"version": 1, "base_commit": "abc", "files": {}}',
    '{"version": 1, "base_commit": "%s", "files": []}',
    '{"version": 1, "base_commit": "%s", "files": {"a.py": {"status": "bogus"}}}',
    '{"version": 1, "base_commit": "%s", "files": {"a.py": {"status": "owned"}}}',
    '{"version": 1, "base_commit": "%s", "files": {"a.py": {"status": "diverged", "reason": "r"}}}',
    '{"version": 1, "base_commit": "%s", "files": {"a.py": {"status": "synced", "reviewed": "yes"}}}',
    '{"version": 1, "base_commit": "%s", "files": {"a.py": {"status": "synced", "todo": "later"}}}',
    '{"version": 1, "base_commit": "%s", "files": {"a.py": {"status": "synced", "reviewed_through": "abc"}}}',
])
def test_malformed_manifest_could_not_run(world, text, capsys):
    up, fork, base = world
    (fork / "engine-sync.json").write_text(text.replace("%s", base), encoding="utf-8")
    assert run(up, fork) == 2
    assert "COULD NOT RUN" in err(capsys)


@pytest.mark.parametrize("bad", ["../escape.py", "/abs.py", "a/../b.py", "C:/x.py", "back\\slash.py", "a.py\nFAKE PASS", "x\x7fy.py"])
def test_manifest_path_traversal_and_control_chars_rejected(world, bad):
    up, fork, base = world
    synced_manifest(fork, base, **{bad: {"status": "synced"}})
    assert run(up, fork) == 2


def make_link(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt" and target.is_dir():
        return subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False).returncode == 0
    return False


def test_link_escaping_brain_dir_is_could_not_run(world, tmp_path):
    up, fork, base = world
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "c.py").write_text("secret\n")
    shutil.rmtree(fork / "pkg")
    if not make_link(fork / "pkg", outside):
        pytest.skip("NOT COVERED: neither a symlink nor a directory junction can be created here")
    synced_manifest(fork, base)
    assert run(up, fork) == 2


def test_file_symlink_escaping_brain_dir_is_could_not_run(world, tmp_path):
    up, fork, base = world
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    (fork / "a.py").unlink()
    if not make_link(fork / "a.py", outside):
        pytest.skip("NOT COVERED: file symlinks cannot be created here (the directory-link test covers the same check)")
    synced_manifest(fork, base)
    assert run(up, fork) == 2


# ---- init / accept / set-base --------------------------------------------------------------------------------------

def test_init_baseline_does_not_pass_until_reviewed(world, capsys):
    up, fork, base = world
    put(fork, "b.py", "different\n")
    (fork / "pkg" / "d.py").unlink()
    assert run(up, fork, "--init") == 0
    assert "1 diverged" in capsys.readouterr().out
    data = json.loads((fork / "engine-sync.json").read_text())
    files = data["files"]
    assert data["base_commit"] == base and files["a.py"] == {"status": "synced"}
    assert files["b.py"]["reviewed"] is False and files["b.py"]["sha256"] == pc.sha(b"different\n")
    assert "baseline divergence recorded" in files["b.py"]["reason"]
    assert files["pkg/d.py"] == {"status": "absent", "reason": "not present in this fork"}
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["b.py"]["note"] == "baseline divergence not reviewed"
    assert run(up, fork, "--init") == 1  # refuses to overwrite
    assert run(up, fork, "--accept", "b.py", "--reason", "deliberate") == 0
    assert run(up, fork) == 0


def test_accept_records_review_and_todo(world, capsys):
    up, fork, base = world
    put(fork, "a.py", "v1\n")
    manifest = synced_manifest(fork, base, **{"a.py": div("v1\n", reviewed=False, reason="baseline")})
    assert run(up, fork) == 1
    assert run(up, fork, "--accept", "a.py", "--reason", "ported fix", "--todo", "port") == 0
    entry = json.loads(manifest.read_text())["files"]["a.py"]
    assert entry["reviewed"] is True and entry["reviewed_through"] == base and entry["todo"] == "port"
    assert entry["reason"] == "ported fix"
    _, rep = report(up, fork, capsys)
    assert rows(rep)["a.py"]["note"] == "TODO: port"
    assert run(up, fork, "--accept", "a.py", "--reason", "fork is ahead", "--todo", "upstream") == 0
    _, rep = report(up, fork, capsys)
    assert rows(rep)["a.py"]["note"] == "TODO: upstream this"
    assert run(up, fork, "--accept", "a.py", "--reason", "done", "--todo", "none") == 0
    assert "todo" not in json.loads(manifest.read_text())["files"]["a.py"]
    assert run(up, fork) == 0


def test_accept_refusals(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    capsys.readouterr()
    assert run(up, fork, "--accept", "b.py", "--reason", "x") == 1  # synced, no --diverge
    assert run(up, fork, "--accept", "nope.py", "--reason", "x") == 1
    assert run(up, fork, "--accept", "a.py") == 1  # reason missing
    assert run(up, fork, "--todo", "port") == 1  # --todo without --accept


def test_accept_diverge_converts_synced_file_without_hand_hashing(world, capsys):
    up, fork, base = world
    manifest = synced_manifest(fork, base)
    put(fork, "a.py", "local fix\n", newline="\r\n")
    assert run(up, fork) == 1
    assert run(up, fork, "--accept", "a.py", "--diverge", "--reason", "fork needs it") == 0
    entry = json.loads(manifest.read_text())["files"]["a.py"]
    assert entry["status"] == "diverged" and entry["sha256"] == pc.sha(b"local fix\n") and entry["reviewed"] is True
    assert run(up, fork) == 0
    assert run(up, fork, "--accept", "a.py", "--diverge", "--reason", "again") == 1  # already diverged
    capsys.readouterr()


def test_set_base_over_unaccepted_diverged_upstream_change_is_refused(world, capsys):
    up, fork, base = world
    put(fork, "a.py", "local\n")
    manifest = synced_manifest(fork, base, **{"a.py": div("local\n", reviewed=False)})
    assert run(up, fork, "--accept", "a.py", "--reason", "deliberate") == 0
    put(up, "a.py", "security fix\n")
    new = commit(up, "fix")
    capsys.readouterr()
    assert run(up, fork, "--set-base", new, "--reason", "claimed port") == 1
    assert "a.py" in err(capsys)
    assert json.loads(manifest.read_text())["base_commit"] == base
    assert run(up, fork) == 1  # still NEEDS PORT
    put(fork, "a.py", "local plus security fix\n")
    assert run(up, fork, "--accept", "a.py", "--reason", "ported the fix") == 0
    assert run(up, fork, "--set-base", new, "--reason", "ported") == 0
    assert run(up, fork) == 0


def test_set_base_refusals_and_success(world, capsys):
    up, fork, base = world
    manifest = synced_manifest(fork, base)
    put(up, "a.py", "ported\n")
    new = commit(up, "change a")
    assert run(up, fork, "--set-base", new, "--reason", "r") == 1  # fork not ported yet
    assert json.loads(manifest.read_text())["base_commit"] == base
    put(fork, "a.py", "ported\n")
    assert run(up, fork, "--set-base", "f" * 40, "--reason", "r") == 2  # unknown commit
    assert run(up, fork, "--set-base", new[:10], "--reason", "ported a") == 0  # short id accepted, stored in full
    data = json.loads(manifest.read_text())
    assert data["base_commit"] == new and data["history"][-1]["reason"] == "ported a"
    assert run(up, fork) == 0
    assert run(up, fork, "--set-base", base, "--reason", "rewind") == 1  # not a descendant of the base
    capsys.readouterr()


def test_set_base_rejects_names_and_trees(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    git(up, "branch", "dead")
    assert run(up, fork, "--set-base", "dead", "--reason", "r") == 2
    assert "branch or tag" in err(capsys)
    assert run(up, fork, "--set-base", "main", "--reason", "r") == 2
    assert run(up, fork, "--set-base", "HEAD~1", "--reason", "r") == 2
    assert run(up, fork, "--set-base", git(up, "rev-parse", "HEAD^{tree}"), "--reason", "r") == 2


def test_set_base_requires_accept_and_keeps_20_history(world):
    up, fork, base = world
    put(fork, "a.py", "v1\n")
    manifest = synced_manifest(fork, base, **{"a.py": div("v1\n")})
    put(fork, "a.py", "v2\n")
    assert run(up, fork, "--set-base", base, "--reason", "r") == 1
    assert run(up, fork, "--accept", "a.py", "--reason", "re-ported") == 0
    data = json.loads(manifest.read_text())
    data["history"] = [{"date": "2026-01-01", "commit": base, "reason": f"round {i}"} for i in range(25)]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert run(up, fork, "--set-base", base, "--reason", "round 25") == 0
    history = json.loads(manifest.read_text())["history"]
    assert len(history) == 20 and history[-1]["reason"] == "round 25" and history[0]["reason"] == "round 6"


def test_init_force_merges_and_keeps_a_backup(world, capsys):
    up, fork, base = world
    put(fork, "b.py", "different\n")
    assert run(up, fork, "--init") == 0
    assert run(up, fork, "--accept", "b.py", "--reason", "hard-won reason", "--todo", "upstream") == 0
    manifest = fork / "engine-sync.json"
    data = json.loads(manifest.read_text())
    data["files"]["a.py"] = {"status": "owned", "reason": "identity gate"}
    data["history"] = [{"date": "2026-01-01", "commit": base, "reason": "first port"}]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    before = manifest.read_bytes()
    put(up, "pkg/e.py", "new engine file\n")
    commit(up, "new file")
    capsys.readouterr()
    assert run(up, fork, "--init", "--force") == 0
    assert (fork / "engine-sync.json.bak").read_bytes() == before
    merged = json.loads(manifest.read_text())
    assert merged["files"]["b.py"]["reason"] == "hard-won reason" and merged["files"]["b.py"]["todo"] == "upstream"
    assert merged["files"]["a.py"] == {"status": "owned", "reason": "identity gate"}
    assert merged["history"] == data["history"] and merged["base_commit"] == base
    assert merged["files"]["pkg/e.py"] == {"status": "absent", "reason": "not present in this fork"}


def test_init_force_with_unreadable_manifest_still_keeps_backup(world, capsys):
    up, fork, _ = world
    (fork / "engine-sync.json").write_text("{broken", encoding="utf-8")
    assert run(up, fork, "--init", "--force") == 0
    assert (fork / "engine-sync.json.bak").read_text() == "{broken"
    assert "unreadable" in capsys.readouterr().out


def test_no_manifest_write_on_could_not_run(world):
    up, fork, base = world
    manifest = synced_manifest(fork, base)
    before = manifest.read_bytes()
    assert run(up, fork, "--set-base", "f" * 40, "--reason", "r") == 2
    assert manifest.read_bytes() == before
    assert not list(fork.glob(".engine-sync-*"))


def test_failed_write_leaves_no_temp_file_and_exits_2(world, monkeypatch, capsys):
    up, fork, base = world
    put(fork, "a.py", "v1\n")
    manifest = synced_manifest(fork, base, **{"a.py": div("v1\n", reviewed=False)})
    before = manifest.read_bytes()

    def refuse(*_args, **_kwargs):
        raise PermissionError("read-only manifest")

    monkeypatch.setattr(pc.os, "replace", refuse)
    assert run(up, fork, "--accept", "a.py", "--reason", "r") == 2
    assert "cannot write manifest" in err(capsys)
    assert manifest.read_bytes() == before and not list(fork.glob(".engine-sync-*"))


# ---- as a real process ---------------------------------------------------------------------------------------------

def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "tools.port_check", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def test_subprocess_exit_codes_and_output(world):
    up, fork, base = world
    synced_manifest(fork, base)
    ok = cli("--brain", str(fork), "--upstream", str(up))
    assert ok.returncode == 0 and "port check: PASS" in ok.stdout
    put(up, "a.py", "changed upstream\n")
    commit(up)
    stale = cli("--brain", str(fork), "--upstream", str(up), "--json")
    assert stale.returncode == 1
    assert json.loads(stale.stdout)["verdict"] == "FAIL" and "NEEDS PORT" in stale.stdout


def test_subprocess_could_not_run_prints_one_line_and_no_traceback(world, tmp_path):
    up, fork, _ = world
    (fork / "engine-sync.json").write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
    bad = cli("--brain", str(fork), "--upstream", str(up))
    assert bad.returncode == 2 and "COULD NOT RUN" in bad.stderr and "Traceback" not in bad.stderr
    missing = cli("--brain", str(tmp_path / "nope"), "--upstream", str(up))
    assert missing.returncode == 2 and "brain directory not found" in missing.stderr


def test_subprocess_init_then_check_fails_until_reviewed(world):
    up, fork, _ = world
    put(fork, "a.py", "drifted\n")
    assert cli("--brain", str(fork), "--upstream", str(up), "--init").returncode == 0
    first = cli("--brain", str(fork), "--upstream", str(up))
    assert first.returncode == 1 and "baseline divergence not reviewed" in first.stdout
