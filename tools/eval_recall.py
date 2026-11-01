"""Retrieval regression check for wiki_context: measure before changing search.

    python -m tools.eval_recall --wiki <a built or pulled silo folder> [--questions <json>] [--k 3] [--min-hit 0.8]

For each question in tests/eval/recall_questions.json (or --questions), runs Wiki.context(q) and records the rank of the first gold
page among the results. Reports hit@k (share of questions with a gold page in the top k) and MRR (mean of 1/rank,
0 when missed), lists every miss, and exits non-zero below --min-hit so it can gate a search change.
Read-only: it never writes to the wiki.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mcp_server.store import CachedStore
from mcp_server.wiki_store import Wiki


class _ReadOnlyDir:
    """A folder as a CachedStore backend, so each page is parsed once (FileStore re-parses on every read, which
    made a 28-question run over 2,000 pages take more than half an hour). Writes are refused: eval never writes."""

    def __init__(self, root: str):
        self.root = Path(root)

    def list_names(self) -> list[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*.md"))

    def get(self, name: str) -> str | None:
        f = self.root / name
        return f.read_text(encoding="utf-8") if f.is_file() else None

    def put(self, name: str, text: str) -> None:
        raise RuntimeError("eval_recall is read-only")


QUESTIONS = Path(__file__).resolve().parents[1] / "tests" / "eval" / "recall_questions.json"


def rank_of(results: list[dict], gold: list[str]) -> int | None:
    for i, r in enumerate(results, 1):
        if r.get("path") in gold:
            return i
    return None


def run(wiki: Wiki, k: int, limit: int = 10, questions: Path = QUESTIONS) -> dict:
    rows = json.loads(Path(questions).read_text(encoding="utf-8"))["questions"]
    out = []
    for row in rows:
        res = wiki.context(row["q"], limit)["results"]
        r = rank_of(res, row["gold"])
        out.append({"q": row["q"], "gold": row["gold"], "rank": r, "top": [x.get("path") for x in res[:k]]})
    n = len(out)
    hit = sum(1 for o in out if o["rank"] and o["rank"] <= k) / n
    mrr = sum(1 / o["rank"] for o in out if o["rank"]) / n
    return {"questions": n, f"hit@{k}": round(hit, 3), "mrr": round(mrr, 3),
            "misses": [o for o in out if not o["rank"] or o["rank"] > k]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wiki", required=True)
    ap.add_argument("--questions", default=str(QUESTIONS))
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--min-hit", type=float, default=0.0)
    a = ap.parse_args(argv)
    r = run(Wiki(CachedStore(_ReadOnlyDir(a.wiki))), a.k, questions=Path(a.questions))
    print(json.dumps(r, indent=1, ensure_ascii=False))
    return 0 if r[f"hit@{a.k}"] >= a.min_hit else 1


if __name__ == "__main__":
    sys.exit(main())
