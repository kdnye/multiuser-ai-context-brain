# ARCHITECTURE

How the brain is built and why. For the idea behind it read [docs/concepts.md](docs/concepts.md); to run it, the
[README](README.md).

**Equations: none.** The code uses no formulas beyond counting and two retrieval metrics in the eval tool
(`tools/eval_recall.py`): hit@k = (questions whose first correct page ranks ≤ k) / (questions), and
MRR = mean over questions of 1/rank (0 when missed). They exist to measure search changes before shipping them.

## Shape
```
user (Claude / ChatGPT / Claude Code) --MCP over HTTPS--> the server (one container)
   EntraGate (entra.py): verify the bearer token -> Identity(oid, email, roles)
   tools (server.py) -> Brain (router.py): check silo access, audit, call that silo's Wiki
   Wiki per silo (wiki_store.py) on CachedStore(GcsBackend(bucket, "silos/<silo>"))      hosted
                                  or FileStore(<BRAIN_ROOT>/<silo>)                       local (stdio)
```

| Module | Responsibility |
|---|---|
| `mcp_server/access.py` | Silo policy: who may read/write which silo; configurable role silos |
| `mcp_server/router.py` | `Brain`: one `Wiki` per silo; every call checked and audited; cross-silo recall over readable silos only; result caps |
| `mcp_server/entra.py` | Entra ID token gate (pure ASGI) and the RFC 9728 discovery document |
| `mcp_server/server.py` | FastMCP server: the tools, identity resolution, JSON logging, HTTP app assembly |
| `mcp_server/wiki_store.py` | The wiki engine: pages, frontmatter, links and typed relations, search and recall, index, hubs, ingest |
| `mcp_server/store.py` | Storage: `FileStore` (folder), `GcsBackend` (Cloud Storage) and `CachedStore` (in-memory cache) |
| `mcp_server/records.py` | Tasks and future needs as validated records; live lists; the `reference/tasks` hub |
| `mcp_server/dates.py` | Activity dates and `wiki_timeline` |
| `mcp_server/employee.py` | Employee wiki: Diataxis sections, app and dashboard cards, role portals, owner/review lint |
| `tools/wiki/` | Scrubber, lint, sync (push/pull/verify/vault), operator CLI |
| `tools/seed.py` | Build a silo folder from reviewed seed pages through the engine |
| `tools/release_check.py` | Public-release gate (local and hashed denylists, generic leak patterns) |
| `tools/eval_recall.py` | Retrieval regression check (hit@k, MRR) |

## Tools (`server.py`)
Every tool acts as the verified caller; `silo` defaults to the caller's own (`me`). Reads may name any silo the caller
can read; writes any silo the caller can write.

| Tool | Does | Key parameters |
|---|---|---|
| `wiki_context` | Recall: best pages across all readable silos, each with neighbours. Call first. | `topic`, `limit` (≤20) |
| `wiki_search` | Keyword search across readable silos; a `YYYY-MM-DD` in the query filters by date | `query`, `limit` (≤50) |
| `wiki_read` | One page plus its `revision` | `path`, `silo` |
| `wiki_index` | A silo's catalog, or one section's, paged | `silo`, `section`, `offset`, `limit` |
| `wiki_list` | Pages in one silo, filtered | `silo`, `section`, `scope`, `date_from`, `date_to`, `sort`, `status`, `not_status`, `limit` (≤500) |
| `wiki_timeline` | One silo's activity by day | `date_from`, `date_to`, `silo`, `scope`, `section`, `per_day`, `compact` |
| `wiki_log` | Recent log entries of one silo | `n`, `since`, `until`, `silo` |
| `wiki_links` | Links, backlinks and typed relations of a page | `path`, `silo` |
| `wiki_write` | Create or replace a page (frontmatter required; scrubbed) | `path`, `content`, `summary`, `silo`, `expected_revision` |
| `wiki_append` | Add a dated section to an existing page | `path`, `text`, `summary`, `heading`, `silo` |
| `wiki_task_update` | Change a task or future need; adds a history line | `path`, `status`, `priority`, `due`, `horizon`, `note`, `silo` |
| `wiki_ingest_session` | Record a session in your own silo (docs/session-capture.md) | `payload` |

## Silos and access (`access.py`)
| Silo | Read | Write |
|---|---|---|
| `users/<oid>` (the caller's object id, a GUID; never the email, which can collide or be reused) | owner | owner |
| `shared` | everyone signed in | role `BRAIN_SHARED_EDITOR_ROLE` (default `shared-editor`) |
| each name in `BRAIN_ROLE_SILOS` (default `finance,hr,ops`) | the role of the same name | the role of the same name |

Anything else is denied. `me` means the caller's own silo. Roles come from the token's `roles` claim (Entra app
roles), so access changes happen in the identity provider, not in code. Role-silo names must be lowercase slugs and
cannot be `users`, `shared`, `me`, `index`, `log`, `brain-user` (every assigned user holds it) or the shared-editor
role; the shared-editor role cannot also be a role silo (a finance member would otherwise edit `shared`). An unsafe
configuration stops the server at start (`validate_config`).

**Why one wiki per silo, not one wiki with a filter:** each silo has its own index, hubs, log and links on its own
store prefix, so nothing a user can read is computed from pages they cannot. Cross-silo recall (`wiki_context`,
`wiki_search`) runs over the caller's readable silos only and labels each result `silo`. A denied silo gives the
same error whether it exists or not, and so does a malformed silo name.

**Audit:** every access decision through the connector (allowed or not, including malformed silo names) is logged as
one JSON line: user id, action, silo, path, allowed. No page content, no email, no token. Operator tools
(`tools.wiki pull|push|vault`) work on the bucket directly and are outside this log (STANDARDS 6b).

## Identity (`entra.py`)
Microsoft identity platform v2 access tokens, RS256 only, signature from the tenant JWKS; `iss` and `tid` must be
the configured tenant; `aud` must be one of `ENTRA_AUDIENCES`; **`scp` must contain one of the scope names this server
advertises** (`ENTRA_SCOPES`, default `brain.access`; the name is the part after the last `/`), because several
connectors can share one app registration and then share the same `aud`, so audience alone cannot say which connector
a token was consented for, and a token with no `scp` (an app-only token) is refused; `exp`/`nbf` with 60 s leeway;
`oid` required and a GUID; email (`preferred_username`/`email`/`upn`) must be ASCII and on an allowed domain. Every
failure is the same 401 with a `WWW-Authenticate` header pointing at `/.well-known/oauth-protected-resource`. Verified identities are
cached until token expiry, keyed by the token's SHA-256, in a bounded cache. The JWKS fetch runs off the event loop.

Local development (stdio only) uses `BRAIN_DEV_EMAIL` / `BRAIN_DEV_ROLES` and derives a stable GUID from the email
(uuid5). The HTTP service refuses to start if `BRAIN_DEV_EMAIL` is set.

Other identity providers (Google, Okta, any OIDC issuer) are not built yet; the gate is small and the `Identity` it
produces is provider-neutral, so adding one is a contained change (docs/roadmap.md).

## Wiki engine (`wiki_store.py`)
- **Pages** are `section/slug` with YAML-style frontmatter (JSON-encoded values). Required: `title`, `type`, `scope`
  (`personal` | `work` | `unscoped`), `source_ids`, `updated` (set by the server). Sections: `projects`, `notes`,
  `sessions`, `repos`, `reference`, `pages`, `concepts`, `answers`, `people`, `tasks`, `future-needs`, plus the
  employee sections (below). Paths and names are validated with `fullmatch`.
- **Links** are Obsidian wikilinks `[[section/slug|Title]]`. **Typed relations** in frontmatter (`project`, `parent`,
  `related`, `supersedes`, `superseded_by`, `depends_on`, `people`, `reports_to`, `session`, `origin_session`) count
  as links, are shown in both directions by `wiki_links`, and are checked by lint.
- **Recall** (`wiki_context`): title matches (including close spelling, so a dictated "Jonn" finds "Jon") lead,
  question words are ignored, then body matches; each hit comes with a few neighbours (links, backlinks, relations).
- **Generated pages** per silo, rebuilt on write and never edited by hand: `index.md` (catalog), `log.md`
  (append-only, `## [YYYY-MM-DD] kind | page`), `reference/start-here` (map), area hubs, a people hub and
  `reference/tasks`. An area hub is `reference/area-<slug>`; two area labels can share a slug ("R&D", "R D"), so a
  hub keeps the slug it already has (found by its title) and only a new area takes the next free one: adding an
  area never moves an existing hub.
- **Ingest** (`wiki_ingest_session`): see [docs/session-capture.md](docs/session-capture.md). One index rebuild and
  one log write per ingest; returns `pages_to_review`.
- **Scrubber** (`tools/wiki/scrub.py`), on bodies and frontmatter: removes emails, phone numbers, SSNs, card numbers,
  labelled account/member/claim/patient numbers and dates of birth. Shared mailboxes (ops@, info@...) are kept.
  Person pages (`people/`) keep email addresses; they keep phone numbers only in the owner's own silo (decided by
  the engine from the silo, never by the caller).

## Tasks and future needs (`records.py`)
`tasks/<slug>` (type `task`: `status`, `priority`, `kind`, `due`, relations `project` and `session`) and
`future-needs/<slug>` (type `future_need`: `horizon`, `status`, relations `project` and `origin_session`). Allowed
values are fixed in `records.ALLOWED`; a direct write with an invented value is refused, an ingest drops and reports
it. Project and session pages show their records live when read (computed, never stored); `wiki_task_update` changes
status, priority, due or horizon and adds a dated history line.

## Dates (`dates.py`)
A page's activity date is `date`, else `created`, else the date ending its first dated source id; never `updated`.
Rewrites keep `date`/`created`; a new page gets `created`. `wiki_timeline(date_from, date_to)` groups a range by day:
pages dated that day, then dated changes (appended `## Heading (YYYY-MM-DD)` sections, task history lines).
`compact=true` gives each day's sessions in full and everything else as counts. Timeline, list and log act on one
silo (the caller's own unless another readable one is named); `wiki_search` with a YYYY-MM-DD runs an exact-date
filter in each readable silo.

## Employee wiki (`employee.py`)
The staff-facing layer, mostly in `shared`. Diataxis splits documents by what the reader is doing:

| Section | For | Examples |
|---|---|---|
| `tutorials/` | learning, start to finish | onboarding week, "your first quote" |
| `how-to/` | doing one task | SOPs, runbooks |
| `reference/` | looking something up | policies, glossaries, who-owns-what |
| `explanation/` | understanding why | how the systems fit together, decisions |
| `apps/` | one card per internal app | purpose, `url`, owner, training, health page |
| `dashboards/` | one card per dashboard or saved report | `url` + `system_role`; the link only, never the data |
| `roles/` | one portal per job function | ops, finance, it, office-manager |

Frontmatter: `owner`, `review_by` (YYYY-MM-DD), `audience` (role slugs or `["all"]`). Writes refuse malformed values
(bad date, non-list audience, non-https url, a dashboard card without `url` and `system_role`); lint reports
`missing_owner`, `missing_review_by`, `bad_review_by` (not YYYY-MM-DD, e.g. after a hand edit), `review_overdue`,
`unknown_audience`. A role portal, when read, lists every page
whose `audience` names it, grouped by kind; it is computed on read and never stored. `audience` is who a page is FOR,
not who may read it: access stays with the silo.

Pages built from documents may carry `source_date`, `material_age` (`current`, `older`, `undated`) and `silo`; the
engine stores them untouched; the page author adds the banner that `older`/`undated` pages open with. See
[docs/document-intake.md](docs/document-intake.md).

## Storage (`store.py`)
`Store` is a small interface (`names`, `read`, `write`), implemented by `FileStore` (a folder) and `CachedStore`, which
wraps a remote `Backend` (`list_names`, `get`, `put`) with an in-memory page cache loaded at first use. Hosted: `GcsBackend` on a **versioned** bucket, one prefix per silo. Because the cache never
expires, the service runs **one instance** and writes made straight to the bucket need a restart (deploy/README.md
step 5). Another backend (S3, Azure Blob, a database) needs only the three `Backend` methods.

## Hardening (security review log)
- **Silo identity** is the object id, not the email: `john.smith@`, `john_smith@` and `john-smith@` used to collapse
  to one silo, and a renamed or reused address would strand or inherit one.
- **Names and paths** use `fullmatch` (`$` accepts a trailing newline, which on object storage would inject lines into
  `index.md`). `source_ids` must fullmatch `[A-Za-z0-9:/._@-]{1,200}`; frontmatter dicts and lists are scrubbed too.
- **Denial of service:** tools run in a worker thread; the token cache is bounded; every scrub/date regex is linear
  (200 KB adversarial inputs run well under 0.5 s; tests pin this); every stored page is capped at 200 KB; limits are
  clamped (context 20, search 50, list 500, log 200, timeline 100 per day and 150 in total).
- **Stale writes:** `wiki_read` returns `revision` (12 hex of the SHA-256 of the stored page); `wiki_write(...,
  expected_revision=...)` is refused with `revision_conflict` if the page changed since.
- **Atomic ingest and locked updates:** `wiki_ingest_session` stages every page and writes only if all succeed;
  `wiki_append` and `wiki_task_update` read, build and store under the silo's write lock, so concurrent calls never
  lose each other's change. `wiki_task_update` refuses fields of the other record type.
- **Log and audit integrity:** summaries are collapsed to one line before they reach `log.md` (no forged entries);
  audit lines log a caller's path or silo only when it is slug-like, otherwise `<invalid>`, and a slug that spells a
  phone number or SSN (`PII_SHAPED` in `router.py`) is logged as `<redacted>`; `wiki_log` is clamped
  to 200 entries. `source_ids` that the scrubber would change (email, phone, ID number) are refused.
- **Round three (adversarial review):**
  - *Regexes:* the wikilink and summary regexes were quadratic on unclosed `[[` (a 12 KB page cost seconds, a 200 KB page
    minutes, and the page persisted in the bucket); every group is now bounded and cannot contain `[` or a newline.
  - *Poison pages:* `title`, `type` and `scope` must be strings, validated before the page is stored (a numeric title
    used to be stored and then broke every later index rebuild in that silo); frontmatter keys must be plain
    identifiers and nested keys are scrubbed.
  - *Scrubber:* also catches separator variants, full-width and zero-width tricks, labelled SSNs, more birth-date
    forms and common credentials (token prefixes, private-key headers, `password=` assignments). Log summaries and
    the titles that become slugs are scrubbed too. It stays a backstop; unlabelled digit runs are left alone on purpose.
    A `password:`-style label masks its value only if the value contains a digit or is a long mixed-case string:
    placeholders (`<name>`, `$VAR`, `${VAR}`, `%VAR%`, `[..]`) and identifiers (`from_secret_manager`) are
    documentation about a secret, so they are left alone (found by scanning the live brains). A long all-lowercase
    passphrase after such a label is therefore not caught.
  - *Reads:* `wiki_read` takes content and revision from one read; computed lists (role portal, project and session
    task lists) are cut when a read page is written back; frontmatter is split on `\n` only (U+2028 injected keys).
  - *Limits:* queries are capped at 300 characters and 12 fuzzy terms; each silo has a page and a size quota
    (`BRAIN_MAX_PAGES`, `BRAIN_MAX_SILO_BYTES`) because one user could otherwise exhaust the single instance's memory.
  - *Identity:* an email with more than one `@` is refused; `BRAIN_REQUIRED_ROLE` optionally demands an app role.
  - *Audit:* the normalized path is logged, not the caller's spelling.
  - *Known limits, not fixed:* `log.md` and `index.md` are rewritten whole on every write and grow with history; a
    rolling deploy can briefly run two instances against the single-writer assumption (storage has no generation
    precondition); dependencies are pinned by range, not by hash.
- **Operator tools:** `pull` refuses object names that would land outside the destination; `verify` fails on extra
  pages (dot-folders such as `.obsidian/` excepted); `vault` refuses an empty bucket listing and a destination with no
  page in common with the bucket, and has `--dry-run`.
- **Errors:** wiki and access refusals come back as data; anything else returns `{"error": "internal error"}` and
  logs only the exception type and tool name.
- **Open: prompt-injected writes.** Text ingested into a person's own silo can steer a session that holds
  `shared-editor` or a role into writing where others read. Options are in docs/roadmap.md.

## Operator tools
`python -m tools.wiki push|pull|vault` act on ONE silo (`--prefix silos/<silo>`); `verify` (`--dest`) and `lint`
(`--wiki`) work on a local folder. There is no cross-silo operator command. `python -m tools.seed` builds a silo from
reviewed pages through the engine before `push`. `vault` writes a silo as an Obsidian vault for read-only browsing.

`python -m tools.port_check --brain <fork>` compares a fork's copies of the engine files (listed in
`tools/engine_files.txt`) with this repo through git and prints one PASS / FAIL / NOT COVERED row per file (exit 2 =
COULD NOT RUN). It reads the fork's `engine-sync.json`; `--init`, `--accept` and `--set-base` maintain that manifest.
It reads files as data and never executes them, and reads upstream at its committed HEAD only.

### Forks and drift control
This repo is canonical. A fork keeps a manifest of how each engine file stands: `synced` (equal to this repo at
`base_commit`), `diverged` (different, pinned by sha256 with a reason; FAILs until a person reviews it with `--accept`), `owned` (a policy file the fork owns,
never compared) or `absent`. Files are compared with CRLF normalised to LF. A change here makes every `synced` file it
touches report `NEEDS PORT` until the fork ports it and moves `base_commit` with `--set-base`. There are no equations;
the only computation is the sha256 of the normalised file. Procedure: [docs/PORTING.md](docs/PORTING.md).
