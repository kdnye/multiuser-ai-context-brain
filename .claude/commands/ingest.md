---
description: Ingest this working session into your own silo of the brain
---

Record what this session produced, following CLAUDE.md and docs/session-capture.md.

1. Build the canonical session JSON (docs/session-capture.md): `meta`, `session` (title "Daily chat on YYYY-MM-DD concerning <project>", date, a 3-6 sentence summary, concerning, cross_related_projects, source_ai), and only the arrays this session produced: `projects`, `notes` (title, tag, project, body), `tasks`, `future_needs`, `maintenance`, `captures`. Never include credentials, ID numbers, personal contact details, health, HR or pay data.
2. Call `wiki_ingest_session` with it. Sessions always land in the caller's own silo. Report what was created and updated, and anything the scrubber removed.
3. Read every page in `pages_to_review` and update it with `wiki_append` or `wiki_write` (pass the page's `revision` as `expected_revision`). Say explicitly when new information contradicts an older page.
4. Run the lint (`python -m tools.wiki lint --wiki <folder>` for a local copy) and fix what is broken. Tasks and future needs become records (deduped by title; report any `dropped_values`); maintenance items stay as follow-ups on the session page. Flag anything urgent to the user.
