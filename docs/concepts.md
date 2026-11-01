# Concepts: why a "context brain", and how to run one

Start here if you want the idea before the code.

## The problem
AI assistants forget. Each chat starts cold, so you re-explain your role, your systems, your decisions and your
preferences, and every assistant (Claude, ChatGPT, a coding agent) builds its own partial picture. Memory features
inside one product help, but they are opaque, per-product, and not shareable with a team.

## The pattern: an LLM-maintained wiki (Karpathy)
Andrej Karpathy described an "LLM wiki": instead of re-reading raw sources on every question (classic RAG), the
model **compiles** what it learns into a linked set of markdown pages and keeps them current. Three layers:

| Layer | What | Who changes it |
|---|---|---|
| Raw sources | Documents, exports, chat transcripts. Immutable. | Nobody (kept outside the wiki) |
| The wiki | One page per thing (project, person, decision, how-to), linked with `[[section/slug|Title]]` | The AI writes; humans read and correct |
| The schema | `CLAUDE.md` / server instructions: page rules, sections, workflows | Humans, deliberately |

Why markdown in files: you can read it without the AI, diff it in git, open it in Obsidian (the links become a
graph), back it up anywhere, and switch AI vendors without a migration.

This repo adds an **MCP server** in front of the wiki, so any MCP-capable assistant reaches the same memory with the
same rules: `wiki_context` (recall first), `wiki_read`, `wiki_search`, `wiki_write`, `wiki_append`,
`wiki_ingest_session`, `wiki_timeline` and friends (ARCHITECTURE "Tools" lists all twelve).

## Multi-user: silos, not filters
A team brain needs privacy that does not depend on the prompt. Each person gets a private silo; a `shared` silo holds
reviewed team knowledge; role silos (finance, HR, operations) are readable only with the matching role from the
identity provider. Each silo is a separate wiki with its own index and search, so a page you cannot read can never
appear in your results, not even as a title. Details: ARCHITECTURE "Silos and access".

## Two brains: personal and work, with stubs and pointers
Most people have a personal life and a job, and the two should not leak into each other. The model this project grew
out of runs **two separate brains**:

- a **personal brain** (single user: health, personal projects, preferences), and
- a **work brain** (this server: your private work silo plus the shared and role silos).

Rules that keep them clean:
1. **One canonical home per fact.** Work detail lives in the work brain, personal detail in the personal brain.
   Never copy a page across.
2. **Stubs and pointers.** When one brain needs to know something exists in the other, write a stub of at most three
   lines (what it is, current state, why it matters) ending in a plain-text pointer such as `work:<path>` or
   `personal:<path>`. Wiki links do not cross servers.
3. **What crosses.** The personal brain gets stubs for active work projects only. The work brain gets only personal
   items that change how work gets done (availability, working style, hard rules); never health, finance or family.
4. **Ask before every write:** "Is this already in the other brain? Then pointer, not copy."
5. **Update stubs with the canonical page.** When a decision or status changes, update the stub in the same session.

The server in this repo can host either brain: run it single-user (a personal deployment where you are the only
assigned user) or multi-user (a team).

## Recall first, then write back
The habit that makes it work, encoded in the server instructions every client receives:
1. **Recall first.** For any "who/what/remember when" question, call `wiki_context(topic)` before anything else, read
   only the pages needed, cite them as `silo:path`, and say so when the brain does not know. Never guess.
2. **Write back what lasts** at the end of a session (`wiki_ingest_session`), then update the existing pages the
   session changed (`pages_to_review`). See [session-capture.md](session-capture.md).
3. **Label uncertainty.** Inferred content is marked (`[Inference]`, `[Unverified]`); generated content is never
   presented as fact.

## Records: tasks and future needs
Tasks (`tasks/<slug>`) and future needs (`future-needs/<slug>`) are pages with validated fields (status, priority,
due, horizon). Project and session pages list their records live when read; `reference/tasks` is the rollup. Change
one with `wiki_task_update`, which adds a dated history line, so "what did I finish last week?" is answerable.

## Dates: "what was I doing around X?"
Every page is dated by its `date`/`created` field (never `updated`) plus its dated history lines.
`wiki_timeline(date_from, date_to)` groups a range by day; `compact=true` fits a week in one reply.

## The employee wiki (Diataxis)
Staff-facing pages in `shared` follow Diataxis: `tutorials/` (learn), `how-to/` (do), `reference/` (look up),
`explanation/` (understand), plus `apps/` and `dashboards/` cards (links only, never data) and `roles/` portals that
list every page written for that role, computed live. Every page carries an `owner` and a `review_by` date; lint
flags stale ones. See ARCHITECTURE "Employee wiki" and `templates/employee/`.

## Onboarding interview
The first time someone connects, the assistant runs a short interview (language, role, tone, length, format, when to
speak up unprompted, how to label uncertainty, what never to save) and writes a `reference/profile` page to their
own silo only after they confirm it. The script is a page in the shared silo, so every AI client runs the same one:
`examples/seed/shared/how-to/brain-onboarding-interview.md`.
