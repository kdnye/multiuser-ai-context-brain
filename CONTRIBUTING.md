# Contributing

Thanks for helping. This project is small on purpose: a few thousand lines of Python, no database, no framework
beyond the MCP SDK. Please keep it that way.

## Before you start
- Read `docs/concepts.md`, then `ARCHITECTURE.md` and `STANDARDS.md`.
- For anything bigger than a bug fix, open an issue first and describe the change, so we agree on the design.
  `docs/roadmap.md` lists open work; items marked **(good first issue)** are self-contained.

## Workflow
1. Fork, then branch from `main` (`feat/<short-name>` or `fix/<short-name>`).
2. Set up: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements-mcp.txt pytest ruff`.
3. Make the change with tests. Every new deny path gets a test; every new regex gets an adversarial-input timing test
   (see `tests/test_security_fixes.py`).
4. Update the docs in the same PR (README, ARCHITECTURE, STANDARDS, CLAUDE.md, `docs/` as relevant).
5. Run, and paste the results in the PR:
   ```bash
   python -m pytest
   ruff check .
   python -m tools.release_check
   gitleaks detect --no-git --source .
   ```
   For search changes, also `python -m tools.eval_recall --wiki <folder>` before and after.
6. Open a pull request. Describe what changed, why, and how you tested it.

## Rules that are not negotiable
- Access control stays in the server (`access.py`, `router.py`), never in prompts.
- One wiki per silo; no cross-silo index, cache or embedding store.
- No real data in the repo: examples use `example.com` and fictional people. The release check fails otherwise.
- No new runtime dependency without discussion in an issue.

## Style
- Python 3.11+, `ruff check .` clean, line length 140.
- Docstrings explain **why** a rule exists, not just what the code does.
- Pages and docs follow STANDARDS "Look and feel": plain English, short sentences, answer first.

## Using an AI assistant to contribute
Welcome. Point it at `CLAUDE.md`. You are responsible for what it writes: read the diff, run the checks, and make
sure it did not invent facts or weaken an access check to make a test pass.
