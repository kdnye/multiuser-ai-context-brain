# CLAUDE.md - multiuser-ai-context-brain

A public, multi-user, AI-maintained wiki behind an MCP server: per-person silos, a shared reference area and
role-gated silos, all enforced in the server. Extracted and sanitized from a live deployment; contributions welcome.

Read first: `README.md` (run, configure, maintain), `ARCHITECTURE.md` (silos, identity, engine), `STANDARDS.md`
(security and look-and-feel rules), `docs/concepts.md` (the idea), `docs/roadmap.md` (what is open).

## Conventions
- Access is decided in `mcp_server/access.py` and enforced in `mcp_server/router.py`. Never add a tool or operator
  path that reads across silos without going through `Brain`, and never trust a silo name from a prompt.
- **This repo is public.** No real organisation names, people, internal URLs, tenant/client/project ids, bucket names
  or wiki content; examples use `example.com` and "Example Co". `python -m tools.release_check` enforces it and runs
  in the test suite. Private terms live in the git-ignored `.release-denylist`; never commit or print them.
- No sensitive data in silos (STANDARDS 4); link to systems of record.
- Employee pages go in the Diataxis section that matches what the reader is doing (ARCHITECTURE "Employee wiki");
  start from `templates/employee/`; set `owner`, `review_by`, `audience`. Dashboards are links only (STANDARDS 6a).
- Allowed record values live only in `mcp_server/records.py` `ALLOWED`; document changes in docs/session-capture.md.
- Docs change in the same PR as the code (STANDARDS 11).
- Before every commit: `python -m pytest`, `ruff check .`, `python -m tools.release_check`,
  `gitleaks detect --no-git --source .`.
- Measure search changes with `python -m tools.eval_recall` before and after.
- git: commit and push as separate commands; never push to `main`; PRs only.
- Cloud: attended only; env changes with `--update-*` flags, never `--set-env-vars`; max-instances stays 1.

## Using the brain from this repo
`.mcp.json` runs the server locally over stdio as a dev user. Slash commands: `/query`, `/save`, `/ingest`, `/lint`
(`.claude/commands/`). Recall first (`wiki_context`), cite `silo:path`, say when the brain does not know.
