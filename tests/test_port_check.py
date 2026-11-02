"""Tests for tools/port_check.py. Upstream and fork are temporary directories built here; no network."""

import json
import subprocess
from pathlib import Path

import pytest

from tools import port_check as pc

ROOT = Path(__file__).resolve().parents[1]
IDENT = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]


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
    for rel in ("a.py", "b.py", "pkg/c.py", "pkg/d.py"):
        put(up, rel, f"print('{rel}')\nline2\n")
        put(fork, rel, f"print('{rel}')\nline2\n")
    base = commit(up, "base")
    return up, fork, base


def synced_manifest(fork: Path, base: str, **overrides) -> Path:
    files = {p: {"status": "synced"} for p in ("a.py", "b.py", "pkg/c.py", "pkg/d.py")}
    files.update(overrides)
    path = fork / "engine-sync.json"
    path.write_text(json.dumps({"version": 1, "base_commit": base, "files": files}), encoding="utf-8")
    return path


def run(up: Path, fork: Path, *extra: str):
    return pc.main(["--brain", str(fork), "--upstream", str(up), *extra])


def report(up: Path, fork: Path, capsys):
    code = run(up, fork, "--json")
    return code, json.loads(capsys.readouterr().out)


def rows(rep):
    return {r["path"]: r for r in rep["rows"]}


def test_engine_list_expands_to_existing_files():
    for line in (ROOT / "tools" / "engine_files.txt").read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            assert list(ROOT.glob(entry)), f"engine_files.txt entry matches nothing: {entry}"


def test_all_synced_passes_and_exits_zero(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    code, rep = report(up, fork, capsys)
    assert code == 0 and rep["verdict"] == "PASS"
    assert {r["verdict"] for r in rep["rows"]} == {"PASS"}
    assert len(rep["rows"]) == 4


def test_json_shape(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    _, rep = report(up, fork, capsys)
    assert set(rep) == {"verdict", "base_commit", "head_commit", "counts", "rows"}
    assert set(rep["counts"]) == {"PASS", "FAIL", "NOT COVERED"}
    assert set(rep["rows"][0]) == {"path", "status", "verdict", "note"}
    assert rep["base_commit"] == base


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
    assert code == 1
    assert rows(rep)["b.py"]["verdict"] == "FAIL" and "UNCLASSIFIED" in rows(rep)["b.py"]["note"]


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
    assert code == 1
    assert rows(rep)["a.py"]["note"] == "NEEDS PORT (2 changed lines)"
    assert rows(rep)["b.py"]["verdict"] == "PASS"


def test_diverged_pinned_passes_and_changed_fails(world, capsys):
    up, fork, base = world
    put(fork, "a.py", "local\n")
    entry = {"status": "diverged", "sha256": pc.sha(b"local\n"), "reason": "fork identity"}
    synced_manifest(fork, base, **{"a.py": entry})
    code, rep = report(up, fork, capsys)
    assert code == 0 and rows(rep)["a.py"]["verdict"] == "PASS"
    put(fork, "a.py", "local, edited\n")
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["a.py"]["note"] == "unreviewed local change"


def test_diverged_with_upstream_change_needs_port(world, capsys):
    up, fork, base = world
    put(fork, "a.py", "local\n")
    entry = {"status": "diverged", "sha256": pc.sha(b"local\n"), "reason": "r"}
    synced_manifest(fork, base, **{"a.py": entry})
    put(up, "a.py", "new upstream\n")
    commit(up)
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["a.py"]["note"].startswith("NEEDS PORT (diverged")


def test_owned_is_not_covered_and_never_compared(world, capsys):
    up, fork, base = world
    put(fork, "a.py", "anything at all\n")
    synced_manifest(fork, base, **{"a.py": {"status": "owned", "reason": "identity gate"}})
    code, rep = report(up, fork, capsys)
    assert code == 0
    assert rows(rep)["a.py"]["verdict"] == "NOT COVERED" and rows(rep)["a.py"]["note"] == "identity gate"
    assert rep["counts"]["NOT COVERED"] == 1


def test_absent_pass_and_fail(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base, **{"a.py": {"status": "absent", "reason": "not used"}})
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["a.py"]["note"] == "expected absent"
    (fork / "a.py").unlink()
    code, rep = report(up, fork, capsys)
    assert code == 0 and rows(rep)["a.py"]["verdict"] == "PASS"


def test_removed_upstream_fails(world, capsys):
    up, fork, base = world
    synced_manifest(fork, base)
    git(up, "rm", "-q", "b.py")
    commit(up, "drop b")
    code, rep = report(up, fork, capsys)
    assert code == 1 and rows(rep)["b.py"]["note"] == "removed upstream"


def test_manifest_key_outside_engine_list_is_reported(world, capsys):
    up, fork, base = world
    put(up, "extra.py", "x\n")
    put(fork, "extra.py", "x\n")
    base = commit(up, "extra")
    synced_manifest(fork, base, **{"extra.py": {"status": "synced"}})
    code, rep = report(up, fork, capsys)
    assert code == 0 and "extra.py" in rows(rep)


def test_glob_does_not_cross_directories(world):
    up, _, _ = world
    put(up, "pkg/sub/e.py", "x\n")
    commit(up)
    files = pc.Upstream(up).engine_files()
    assert files == ["a.py", "b.py", "pkg/c.py", "pkg/d.py"]


def test_could_not_run_cases(world, tmp_path, capsys):
    up, fork, base = world
    manifest = synced_manifest(fork, base)
    assert run(tmp_path / "missing", fork) == 2  # upstream not a directory
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run(plain, fork) == 2  # not a git repository
    assert pc.main(["--brain", str(tmp_path / "nope"), "--upstream", str(up)]) == 2
    bad = json.loads(manifest.read_text())
    bad["base_commit"] = "0" * 40
    manifest.write_text(json.dumps(bad))
    assert run(up, fork) == 2  # base commit not in upstream
    capsys.readouterr()
    assert run(up, fork, "--json") == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "COULD NOT RUN"


@pytest.mark.parametrize("text", [
    "{not json",
    "[]",
    '{"version": 2, "base_commit": "%s", "files": {}}',
    '{"version": 1, "base_commit": "abc", "files": {}}',
    '{"version": 1, "base_commit": "%s", "files": []}',
    '{"version": 1, "base_commit": "%s", "files": {"a.py": {"status": "bogus"}}}',
    '{"version": 1, "base_commit": "%s", "files": {"a.py": {"status": "owned"}}}',
    '{"version": 1, "base_commit": "%s", "files": {"a.py": {"status": "diverged", "reason": "r"}}}',
])
def test_malformed_manifest_could_not_run(world, text):
    up, fork, base = world
    (fork / "engine-sync.json").write_text(text.replace("%s", base) if "%s" in text else text, encoding="utf-8")
    assert run(up, fork) == 2


@pytest.mark.parametrize("bad", ["../escape.py", "/abs.py", "a/../b.py", "C:/x.py", "back\\slash.py"])
def test_manifest_path_traversal_rejected(world, bad):
    up, fork, base = world
    synced_manifest(fork, base, **{bad: {"status": "synced"}})
    assert run(up, fork) == 2


def test_symlink_escaping_brain_dir_is_could_not_run(world, tmp_path):
    up, fork, base = world
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    (fork / "a.py").unlink()
    try:
        (fork / "a.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    synced_manifest(fork, base)
    assert run(up, fork) == 2


def test_init_classifies_and_refuses_overwrite(world, capsys):
    up, fork, base = world
    put(fork, "b.py", "different\n")
    (fork / "pkg" / "d.py").unlink()
    assert run(up, fork, "--init") == 0
    assert "1 diverged" in capsys.readouterr().out
    data = json.loads((fork / "engine-sync.json").read_text())
    assert data["base_commit"] == base and data["version"] == 1
    files = data["files"]
    assert files["a.py"] == {"status": "synced"}
    assert files["b.py"]["status"] == "diverged" and files["b.py"]["sha256"] == pc.sha(b"different\n")
    assert "baseline divergence recorded" in files["b.py"]["reason"]
    assert files["pkg/d.py"] == {"status": "absent", "reason": "not present in this fork"}
    assert run(up, fork, "--init") == 1
    assert run(up, fork, "--init", "--force") == 0
    # the fresh baseline checks clean
    assert run(up, fork) == 0


def test_accept_repins_and_refuses_other_statuses(world, capsys):
    up, fork, base = world
    put(fork, "a.py", "v1\n")
    entry = {"status": "diverged", "sha256": pc.sha(b"v1\n"), "reason": "old"}
    manifest = synced_manifest(fork, base, **{"a.py": entry})
    put(fork, "a.py", "v2\n")
    assert run(up, fork) == 1
    assert run(up, fork, "--accept", "a.py", "--reason", "ported fix") == 0
    data = json.loads(manifest.read_text())
    assert data["files"]["a.py"]["sha256"] == pc.sha(b"v2\n") and data["files"]["a.py"]["reason"] == "ported fix"
    assert run(up, fork) == 0
    assert run(up, fork, "--accept", "b.py", "--reason", "x") == 1  # synced
    assert run(up, fork, "--accept", "nope.py", "--reason", "x") == 1  # not listed
    assert run(up, fork, "--accept", "a.py") == 1  # reason missing
    capsys.readouterr()


def test_set_base_refusals_and_success(world, capsys):
    up, fork, base = world
    manifest = synced_manifest(fork, base)
    put(up, "a.py", "ported\n")
    new = commit(up, "change a")
    assert run(up, fork, "--set-base", new, "--reason", "r") == 1  # fork not ported yet
    assert json.loads(manifest.read_text())["base_commit"] == base
    put(fork, "a.py", "ported\n")
    assert run(up, fork, "--set-base", "f" * 40, "--reason", "r") == 2  # unknown commit
    assert run(up, fork, "--set-base", new, "--reason", "ported a") == 0
    data = json.loads(manifest.read_text())
    assert data["base_commit"] == new and data["history"][-1]["reason"] == "ported a"
    assert run(up, fork) == 0
    capsys.readouterr()


def test_set_base_requires_accepted_diverged_files_and_keeps_20_history(world):
    up, fork, base = world
    put(fork, "a.py", "v1\n")
    entry = {"status": "diverged", "sha256": pc.sha(b"v1\n"), "reason": "r"}
    manifest = synced_manifest(fork, base, **{"a.py": entry})
    put(fork, "a.py", "v2\n")
    assert run(up, fork, "--set-base", base, "--reason", "r") == 1
    assert run(up, fork, "--accept", "a.py", "--reason", "re-ported") == 0
    for i in range(22):
        assert run(up, fork, "--set-base", base, "--reason", f"round {i}") == 0
    history = json.loads(manifest.read_text())["history"]
    assert len(history) == 20 and history[-1]["reason"] == "round 21"


def test_no_manifest_write_on_could_not_run(world):
    up, fork, base = world
    manifest = synced_manifest(fork, base)
    before = manifest.read_bytes()
    assert run(up, fork, "--set-base", "f" * 40, "--reason", "r") == 2
    assert manifest.read_bytes() == before
    assert not list(fork.glob(".engine-sync-*"))
