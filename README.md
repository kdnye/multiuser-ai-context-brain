# Multi-user AI context brain

A shared, AI-maintained knowledge base that any MCP-capable assistant (Claude, ChatGPT, Claude Code) can read and
write, with per-person privacy enforced by the server. It applies Andrej Karpathy's "LLM wiki" idea: instead of
re-explaining yourself to every chat, the assistant compiles what it learns into small, linked markdown pages and
keeps them current, so every session starts with the context it needs.

- **Your own silo**: private notes, session logs, tasks and decisions. Only you can read it.
- **A `shared` silo**: reviewed team knowledge (how-tos, references, role portals). Everyone reads; editors write.
- **Role silos** (default `finance`, `hr`, `ops`): readable only with the matching role from your identity provider.
- **Plain markdown** with Obsidian-style `[[links]]`: readable without the AI, diffable, portable between vendors.

New here? Read [docs/concepts.md](docs/concepts.md) first (5 minutes): the pattern, silos, the personal/work
"two brains" model, recall-first habits and session capture.

**Status:** extracted from a live deployment (a multi-user work brain behind Microsoft Entra ID, signed in from
Claude, plus a single-user personal brain of a few thousand pages on the same engine). ChatGPT as a client is not yet
verified. See [docs/roadmap.md](docs/roadmap.md).

## What is in here
| Path | What |
|---|---|
| `mcp_server/` | The MCP server: access policy, silo router, Entra gate, wiki engine, storage, records, dates, employee wiki |
| `tools/wiki/` | Scrubber, lint, sync between a folder and the bucket (push/pull/verify/vault), operator CLI |
| `tools/seed.py` | Builds a silo folder from reviewed pages through the engine |
| `tools/release_check.py` | Public-release gate: blocks private identifiers and real email addresses |
| `tools/eval_recall.py` | Retrieval regression check (hit@k, MRR) |
| `examples/seed/shared/` | A fictional "Example Co" shared silo: role portals, getting-started tutorial, connector how-to, onboarding interview, silo reference |
| `templates/employee/` | Starter page per employee section (tutorial, how-to, reference, explanation, app, dashboard, role) |
| `.claude/commands/` | Claude Code slash commands: `/query`, `/save`, `/ingest`, `/lint` |
| `skills/brain-onboarding/` | A thin skill that runs the onboarding interview stored in the shared silo |
| `deploy/` | Cloud Run + Cloud Storage runbook and the Entra/connector setup with lessons learned |
| `docs/` | Concepts, session capture, document intake, roadmap |
| `tests/` | About 260 tests, including every access-deny path and adversarial-input timing |

## Quick start: run it locally (10 minutes, no cloud)
Needs Python 3.11+ and git. Running the server and the tests does not need anything else. **Contributors also need
[gitleaks](https://github.com/gitleaks/gitleaks#installing)** (e.g. `brew install gitleaks`, or a release binary on your
PATH): the pre-commit checklist in [CONTRIBUTING.md](CONTRIBUTING.md) runs it, so install it before your first commit.
```bash
git clone https://github.com/kdnye/multiuser-ai-context-brain && cd multiuser-ai-context-brain
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-mcp.txt pytest ruff
python -m pytest                     # all green before you change anything
python -m tools.seed --src examples/seed/shared --dest local-silos/shared   # a local shared silo to play with
```
Connect it to **Claude Code**: start `claude` in this folder with the virtual environment active (so `python` is the
venv's), and approve the project's MCP server when asked. `.mcp.json` starts the server over stdio as the dev user
`you@example.com` with the `shared-editor` role, silos under `local-silos/`. The server writes one short `access` line
to stderr per access decision; that is the audit log, not an error.
Then try:

> Use the context brain: what should I read to get started? Then run my brain onboarding.

Or run it by hand (any MCP client that speaks stdio):
```bash
BRAIN_DEV_EMAIL=you@example.com BRAIN_DEV_ROLES=shared-editor python -m mcp_server.server
```
Your private silo appears as `local-silos/users/<guid>/`; open `local-silos/` in Obsidian to browse the pages.

## Run it for a team (hosted)
The tested setup is Google Cloud Run + a versioned Cloud Storage bucket + Microsoft Entra ID, and costs very little
at team scale (one small instance). That is the one stack this repo has been run on, not the only possible one. The engine
itself does not depend on Google or Microsoft: storage is a backend in `mcp_server/store.py` and sign-in is the gate in
`mcp_server/entra.py`. Moving to another bucket service or identity provider means writing a replacement for those
two modules and changing the hosted-app wiring in `mcp_server/server.py`, which today requires `BRAIN_STORE=gcs` and
the Entra settings. Local stdio mode needs neither. Follow [deploy/README.md](deploy/README.md) and
[deploy/connectors.md](deploy/connectors.md). Staff then follow the seeded how-to
`how-to/connect-the-brain-to-claude-and-chatgpt`.

## Configuration
Every variable the code reads. The hosted service refuses to start if a required one is missing.

| Variable | Mode | Required | Meaning |
|---|---|---|---|
| `MCP_TRANSPORT` | both | no | `stdio` (default, local) or `http` (hosted) |
| `MCP_PUBLIC_URL` | http | yes | The service's public base URL, e.g. `https://brain.example.com` (discovery document, host check) |
| `ENTRA_TENANT_ID` | http | yes | Your Entra tenant id |
| `ENTRA_AUDIENCES` | http | yes | Comma list: the API registration's client id and/or its App ID URI |
| `ENTRA_SCOPES` | http | no | Scopes advertised in discovery. Default `<MCP_PUBLIC_URL>/mcp/brain.access`, the form that works with MCP clients (deploy/connectors.md "Lessons learned") |
| `ALLOWED_EMAIL_DOMAINS` | http | yes | Comma list of email domains allowed to sign in, e.g. `example.com` |
| `BRAIN_STORE` | both | http: yes | `file` (default) or `gcs`; http requires `gcs` |
| `BRAIN_BUCKET` | http | yes | Cloud Storage bucket; silos live under `silos/<silo>/` |
| `BRAIN_ROOT` | stdio | no | Folder for local silos (default `local-silos`) |
| `BRAIN_DEV_EMAIL` | stdio | yes for stdio | The local dev user. Never set on http (the service refuses to start) |
| `BRAIN_DEV_ROLES` | stdio | no | Comma list of roles for the dev user, e.g. `shared-editor,finance` |
| `BRAIN_ROLE_SILOS` | both | no | Comma list of role-gated silos (default `finance,hr,ops`; empty string for none). Lowercase slugs |
| `BRAIN_SHARED_EDITOR_ROLE` | both | no | The role that may write `shared` (default `shared-editor`) |
| `BRAIN_NAME` | both | no | Display name in the server instructions (default `Context Brain`) |
| `BRAIN_SERVER_NAME` | both | no | MCP server name (default `context-brain`) |
| `BRAIN_MAX_PAGES` | both | no | Page limit per silo (default 5000). A new page past it is refused; edits still work |
| `BRAIN_MAX_SILO_BYTES` | both | no | Size limit per silo, in characters of page text (default 40,000,000). A cached page costs about 10x its size in memory, so size this to the instance (a 512 MiB instance holds roughly 40 MB) |
| `BRAIN_REQUIRED_ROLE` | http | no | An app role every token must carry (for example `brain-user`). Off by default; turn it on as defence in depth in case Entra's "Assignment required" setting is ever disabled |
| `BRAIN_GCS_TOKEN` | tools | no | An OAuth access token for the operator CLI (`gcloud auth print-access-token`); otherwise application default credentials |
| `PORT` | http | no | Listen port (default 8080; Cloud Run sets it) |

Roles are identity-provider app roles: `shared-editor` plus one per role silo. A role grants read/write on its own
silo only.

## Use it (day to day)
- **Ask**: "What do we know about X?" The assistant calls `wiki_context` first and cites `silo:path`.
- **Save a session**: "save this session" (or `/ingest` in Claude Code). See [docs/session-capture.md](docs/session-capture.md).
- **Dates**: "What was I working on last Tuesday?" uses `wiki_timeline`.
- **Tasks**: session tasks become records; "mark the fee-rules task done" uses `wiki_task_update`.
- **Share**: write a page in your own silo, then ask a shared editor to publish it.

## Maintain it
| When | Do |
|---|---|
| Every change | `python -m pytest`, `ruff check .`, `python -m tools.release_check`, `gitleaks detect --no-git --source .` |
| Search change | `python -m tools.eval_recall --wiki <folder> [--questions <file>]` before and after (STANDARDS 13). Your own question set is JSON: `{"questions": [{"q": "<a real question>", "gold": ["<section/slug that must come back>"]}]}`; keep it private |
| Monthly | Pull each silo you administer and lint it: `python -m tools.wiki pull --bucket <BUCKET> --prefix silos/<silo> --dest <folder>`, then `python -m tools.wiki lint --wiki <folder>` (exit 1 when there are findings; fix broken links, orphans, `missing_owner`, `bad_review_by`, `review_overdue`) |
| Before a secret expires | Rotate the AI client's OAuth secret in Entra and re-enter it in the product (deploy/connectors.md) |
| Backup / restore | The bucket is versioned; `python -m tools.wiki pull` for an offline copy; restore steps in deploy/README.md |
| Mirroring a silo to Obsidian | `python -m tools.wiki vault --bucket <BUCKET> --prefix silos/<silo> --dest <vault> [--dry-run]`. It deletes `.md` files in `--dest` that are not in the bucket, so it refuses an empty bucket listing and a folder that shares no page with the bucket (`--force` overrides the second; `--dry-run` shows the changes first) |
| Upgrading `mcp` | It is pinned below 2.0 on purpose (2.0 renamed FastMCP); test before raising the pin |

Keeping your organisation's identifiers out of a fork: list them, one per line, in the git-ignored
`.release-denylist` (names, domains, project ids, people). The committed `tools/release_denylist.sha256` is only for
long, unguessable phrases: a short term's hash can be reversed by guessing. Details: `python -m tools.release_check --help`.

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md). Design: [ARCHITECTURE.md](ARCHITECTURE.md).
Rules: [STANDARDS.md](STANDARDS.md). AI assistants working in this repo start at [CLAUDE.md](CLAUDE.md).

## License
[MIT](LICENSE).
