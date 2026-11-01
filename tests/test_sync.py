"""Tests for tools.wiki.sync (seed the bucket, pull for the mirror, verify)."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_store import FakeBackend
from tools.wiki.__main__ import main
from tools.wiki.sync import MANIFEST, pull, push, verify


def make_wiki(root, pages):
    for name, text in pages.items():
        f = root / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    (root / "notes" / "ignore.txt").parent.mkdir(parents=True, exist_ok=True)
    (root / "notes" / "ignore.txt").write_text("not a page", encoding="utf-8")
    return root


def test_push_seeds_an_empty_bucket(tmp_path):
    wiki = make_wiki(tmp_path / "w", {"index.md": "idx", "notes/a.md": "A", "projects/p.md": "P"})
    b = FakeBackend()
    assert push(wiki, b) == {"uploaded": 3, "skipped_existing": 0, "unchanged": 0}
    assert b.pages == {"index.md": "idx", "notes/a.md": "A", "projects/p.md": "P"}     # only .md files


def test_push_never_clobbers_cloud_edits_unless_forced(tmp_path):
    wiki = make_wiki(tmp_path / "w", {"notes/a.md": "LOCAL OLD", "notes/b.md": "B", "notes/same.md": "S"})
    b = FakeBackend({"notes/a.md": "CLOUD NEWER", "notes/same.md": "S"})
    assert push(wiki, b) == {"uploaded": 1, "skipped_existing": 1, "unchanged": 1}
    assert b.pages["notes/a.md"] == "CLOUD NEWER" and b.pages["notes/b.md"] == "B"
    assert push(wiki, b, force=True)["uploaded"] == 1 and b.pages["notes/a.md"] == "LOCAL OLD"


def test_pull_writes_pages_and_a_manifest_and_verify_passes(tmp_path):
    b = FakeBackend({"index.md": "idx", "notes/a.md": "héllo", "projects/p.md": "P"})
    dest = tmp_path / "mirror"
    result = pull(b, dest)
    assert result["pages"] == 3 and (dest / "notes" / "a.md").read_text(encoding="utf-8") == "héllo" and (dest / MANIFEST).exists()
    v = verify(dest)
    assert v == {"pages": 3, "missing": [], "altered": [], "extra": [], "ok": True}


def test_verify_catches_missing_altered_and_extra_pages(tmp_path):
    b = FakeBackend({"notes/a.md": "A", "notes/b.md": "B", "notes/c.md": "C"})
    dest = tmp_path / "mirror"
    pull(b, dest)
    (dest / "notes" / "a.md").write_text("TAMPERED", encoding="utf-8")
    (dest / "notes" / "b.md").unlink()
    (dest / "notes" / "stray.md").write_text("extra", encoding="utf-8")
    v = verify(dest)
    assert v["altered"] == ["notes/a.md"] and v["missing"] == ["notes/b.md"] and v["extra"] == ["notes/stray.md"] and v["ok"] is False


def test_pull_skips_a_page_deleted_between_list_and_get(tmp_path):
    class Racy(FakeBackend):
        def get(self, name):
            return None if name == "notes/gone.md" else super().get(name)

    r = pull(Racy({"notes/a.md": "A", "notes/gone.md": "G"}), tmp_path / "m")
    assert r["pages"] == 1 and not (tmp_path / "m" / "notes" / "gone.md").exists()


def test_cli_push_pull_verify(tmp_path, capsys):
    wiki = make_wiki(tmp_path / "w", {"index.md": "idx", "notes/a.md": "A"})
    shared = FakeBackend()
    factory = lambda bucket, prefix: shared
    assert main(["push", "--wiki", str(wiki), "--bucket", "b", "--prefix", "silos/users/x"], backend_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)["uploaded"] == 2
    dest = tmp_path / "mirror"
    assert main(["pull", "--bucket", "b", "--prefix", "silos/users/x", "--dest", str(dest)], backend_factory=factory) == 0
    assert main(["verify", "--dest", str(dest)]) == 0
    (dest / "notes" / "a.md").write_text("x", encoding="utf-8")
    capsys.readouterr()
    assert main(["verify", "--dest", str(dest)]) == 1
    assert json.loads(capsys.readouterr().out)["altered"] == ["notes/a.md"]


def test_vault_mirrors_in_place_and_keeps_obsidian_settings(tmp_path):
    from tools.wiki.sync import vault

    b = FakeBackend()
    b.put("index.md", "# index\n")
    b.put("notes/a.md", "A v1\n")
    b.put("notes/b.md", "B\n")
    dest = tmp_path / "vault"
    (dest / ".obsidian").mkdir(parents=True)
    (dest / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (dest / ".obsidian" / "notes.md").write_text("plugin file", encoding="utf-8")   # under a dot-folder: never touched
    first = vault(b, dest)
    assert first == {"pages": 3, "written": 3, "unchanged": 0, "removed": []}
    b.put("notes/a.md", "A v2\n")
    b.pages.pop("notes/b.md")
    (dest / "notes" / "stray.md").write_text("local edit", encoding="utf-8")
    second = vault(b, dest)
    assert second["written"] == 1 and second["unchanged"] == 1 and second["removed"] == ["notes/b.md", "notes/stray.md"]
    assert (dest / "notes" / "a.md").read_text(encoding="utf-8") == "A v2\n"
    assert (dest / ".obsidian" / "app.json").exists() and (dest / ".obsidian" / "notes.md").exists()
    assert verify(dest)["ok"]


def test_lint_cli_runs_the_employee_checks(tmp_path, capsys):
    from tools.seed import build
    seed = Path(__file__).resolve().parents[1] / "examples" / "seed" / "shared"
    build(seed, tmp_path / "s")
    assert main(["lint", "--wiki", str(tmp_path / "s")]) == 0                     # the example seed is clean
    page = tmp_path / "s" / "how-to" / "ask-for-a-page-in-the-shared-area.md"
    page.write_text(page.read_text(encoding="utf-8").replace('review_by: "2027-06-30"', 'review_by: "2020-01-01"'), encoding="utf-8")
    capsys.readouterr()
    assert main(["lint", "--wiki", str(tmp_path / "s")]) == 1
    assert json.loads(capsys.readouterr().out)["by_kind"] == {"review_overdue": 1}
