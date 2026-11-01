"""The example retrieval set passes over the example seed, so a search change that breaks recall fails the suite."""

from pathlib import Path

from mcp_server.store import CachedStore
from mcp_server.wiki_store import Wiki
from tools.eval_recall import _ReadOnlyDir, run
from tools.seed import build

SEED = Path(__file__).resolve().parents[1] / "examples" / "seed" / "shared"


def test_example_recall_set_hits_at_3(tmp_path):
    build(SEED, tmp_path / "shared")
    r = run(Wiki(CachedStore(_ReadOnlyDir(tmp_path / "shared"))), 3)
    assert r["hit@3"] >= 0.85, r["misses"]
