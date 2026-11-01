# Roadmap and open questions

Good first contributions are marked **(good first issue)**. Nothing here is built unless it says so. Open an issue
before starting anything large, so we agree on the design first.

## Verified so far
- Multi-user server with per-person, shared and role silos; Entra ID sign-in from Claude (custom connector, own OAuth
  client) verified in a production tenant, 2026-10.
- The same engine has run a single-user personal brain of a few thousand pages since 2026-10.

## Not yet verified
- **ChatGPT** as a client (deploy/connectors.md section 3 is a checklist from OpenAI's docs).
- A full live multi-user isolation run in a second tenant (the release checks in deploy/README.md step 6).

## Security design, open
1. **Prompt-injected writes.** Ingested text in a person's own silo can steer a session that holds `shared-editor`
   or a role into writing where others read. Options: (a) a propose-only `wiki_propose` into a review queue for
   `shared` and role silos; (b) a confirm token for any write outside the home silo; (c) server-set origin labels
   (`user_said | agent_written | external`) plus a lint for instruction-like text in `external` pages; (d) an alert
   on writes outside the home silo. Recommendation: (c) first, then (a).
2. **Per-silo retrieval eval with a "must not appear" list** to catch cross-silo leakage, alongside the router tests.

## Features, small and independent
3. **One-line `summary:` per page**, shown in search, list and context results, so the model picks pages without
   opening them. **(good first issue)**
4. **MCP tool annotations** (read-only / destructive / idempotent) on every tool, and `max_chars`/`offset` on
   `wiki_read`. **(good first issue)**
5. **`wiki_changes(since)`** and a token budget on `wiki_context`, for session-start hooks.
6. **Another identity provider**: a Google or generic OIDC gate producing the same `Identity`. The personal deployment
   this grew from used a Google gate with an audience check; porting it is a contained change.
7. **Another storage backend** (S3, Azure Blob): three methods (ARCHITECTURE "Storage").

## Bigger moves
8. **Compiled truth above a dated timeline** on people, project and decision pages: a current summary at the top,
   rewritten when evidence changes; appends go below a marker; lint flags a summary older than its newest entry.
9. **Hybrid search** (lexical + embeddings, reciprocal-rank fusion), per silo only, measured with `tools.eval_recall`.
   Never one cross-silo index.
10. **Weekly consolidation report**: a scheduled job writes one review page (near-duplicates, stale summaries,
    contradictions) for a human to accept. No automatic rewrites.
11. **A gateway MCP for internal apps** (one connector over several line-of-business apps, read-only first, writes by
    propose/apply), sharing this repo's identity gate.

## Not recommended
A graph database (a typed-relation markdown wiki of a few thousand pages per silo does not need one); copying AGPL
code into this repo (learn from it instead); one shared vector index across silos.
