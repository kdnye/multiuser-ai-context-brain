"""The example seed for the shared silo builds clean through the engine and every role portal lists its pages."""

from pathlib import Path

from mcp_server.wiki_store import Wiki
from tools.seed import build

SEED = Path(__file__).resolve().parents[1] / "examples" / "seed" / "shared"
PILOT = ("ops", "finance", "it", "office-manager")


def test_seed_builds_clean_and_portals_are_live(tmp_path):
    r = build(SEED, tmp_path / "shared")
    assert r["pages"] == len(list(SEED.rglob("*.md"))) and r["lint"]["by_kind"] == {}   # every seed page builds, lint clean
    # Deleting a reviewed page must fail the build; adding one is allowed. Add new pages to tests/seed_manifest.txt.
    manifest = {ln.strip() for ln in (Path(__file__).parent / "seed_manifest.txt").read_text(encoding="utf-8").splitlines() if ln.strip()}
    present = {p.relative_to(SEED).as_posix() for p in SEED.rglob("*.md")}
    assert manifest <= present, f"seed pages missing: {sorted(manifest - present)}"
    w = Wiki(tmp_path / "shared")
    for role in PILOT:
        portal = w.read(f"roles/{role}")
        assert "## Everything for this role (live:" in portal
        assert "[[tutorials/getting-started-with-the-brain|" in portal      # `all` pages reach every portal
        assert "[[how-to/connect-the-brain-to-claude-and-chatgpt|" in portal
    assert "[[apps/example-quotes|" in w.read("roles/finance") and "[[apps/example-quotes|" not in w.read("roles/it")
    assert "[[dashboards/open-orders|" in w.read("roles/ops")
    start = w.read("reference/start-here")
    assert "## For staff" in start and all(f"[[roles/{r}|" in start for r in PILOT)
