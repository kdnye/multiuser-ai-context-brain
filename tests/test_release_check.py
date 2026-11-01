from pathlib import Path

from tools.release_check import HASHES, LOCAL, digest, main, scan

ROOT = Path(__file__).resolve().parents[1]


def test_repo_is_clean_for_public_release():
    assert scan(ROOT) == []


def test_hashed_terms_match_across_spacing_and_case(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / HASHES).write_text(f"{digest('acme corp')}  # org name\n{digest('acme.internal')}\n", encoding="utf-8")
    (tmp_path / "a.md").write_text("Built at ACME   Corp.\nok line\nsee https://wiki.acme.internal/x\n", encoding="utf-8")
    hits = scan(tmp_path)
    assert [(r, n) for r, n, _ in hits] == [("a.md", 1), ("a.md", 3)]


def test_local_denylist_and_real_email_domains(tmp_path):
    (tmp_path / LOCAL).write_text("# private\nsecret-project\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 'Secret-Project'\nmail = 'pat@example.com'\nreal = 'pat@realcorp.io'\n", encoding="utf-8")
    hits = scan(tmp_path)
    assert ("b.py", 1, "local denylist term 'secret-project'") in hits
    assert ("b.py", 3, "email on a real domain (realcorp.io)") in hits
    assert not any(n == 2 for _, n, _ in hits)


def test_hash_helper_prints_the_digest(capsys):
    assert main(["--hash", "Acme  Corp"]) == 0
    assert capsys.readouterr().out.strip() == digest("acme corp")


def test_terms_wrapped_joined_or_accented_are_caught(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / HASHES).write_text(digest("acme corp") + "\n", encoding="utf-8")
    (tmp_path / LOCAL).write_text("globex\nbig secret plan\n", encoding="utf-8")
    (tmp_path / "a.md").write_text("Made by Acme\nCorp in 2020.\nAlso acmecorp and Glöbex.\nThe big secret\nplan.\n"
                                   "offsite globexx\n", encoding="utf-8")
    kinds = {(n, what) for _, n, what in scan(tmp_path)}
    assert (0, "hashed denylist term (across a line break)") not in kinds          # line 3 already matched in the file
    assert (3, "hashed denylist term") in kinds and (3, "local denylist term 'globex'") in kinds
    assert (0, "local denylist term 'big secret plan' (across a line break)") in kinds
    assert not any(n == 6 for n, _ in kinds)                                         # tokens, not substrings, for short terms


def test_generic_leaks_need_no_list(tmp_path):
    lines = ["id 3f2a9c4e-7b1d-4e8a-9c2f-5d6e7f8a9b0c", "ok a0000000-0000-4000-8000-000000000001",
             "url https://svc-123.us-central1.run.app/mcp", "ok https://brain.example.run.app",
             "bucket gs://acme-silos", "ok gs://<BUCKET>", "project 348120401999", "call 602-555-0100", "ok 555-555-0142"]
    (tmp_path / "c.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "d.bin").write_bytes(b"\xff\xfe\x00bad")
    hit_lines = {n for r, n, _ in scan(tmp_path) if r == "c.md"}
    assert hit_lines == {1, 3, 5, 7, 8}
    assert ("d.bin", 0, "not UTF-8 text: cannot be checked, review by hand or remove") in scan(tmp_path)


def test_help_prints_usage_without_scanning(capsys):
    assert main(["--help"]) == 0 and "Public-release gate" in capsys.readouterr().out
