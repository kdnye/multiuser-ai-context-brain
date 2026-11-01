# Session capture: the canonical session JSON

At the end of a working session, the assistant records what happened with one call:
`wiki_ingest_session(payload)`. The session always lands in the caller's own silo. In Claude Code, `/ingest` does
this for you (`.claude/commands/ingest.md`).

## The payload
Omit any array you have nothing for. Never include credentials, ID numbers, personal contact details, health, HR or
pay data; the scrubber is a backstop, not the control.

```json
{
  "meta": {"source": "claude.ai", "context": "optional free text", "default_type": "Business"},
  "session": {
    "title": "Daily chat on 2026-11-01 concerning Quotes rollout",
    "date": "2026-11-01",
    "source_ai": "Claude",
    "summary": "Three to six sentences: what was decided, what changed, what is next.",
    "concerning": "Quotes rollout",
    "cross_related_projects": ["Pricing review"]
  },
  "projects": [{"name": "Quotes rollout", "summary": "One line.", "type": "Business", "status": "In progress"}],
  "notes": [{"title": "Fee rules", "tag": ["Reference"], "project": "Quotes rollout", "body": "Markdown body."}],
  "tasks": [{"task": "Publish the fee rules page", "project": "Quotes rollout", "priority": "High",
             "status": "Not started", "due": "2026-11-07", "type": "Business", "notes": "optional"}],
  "future_needs": [{"name": "Tiered fees", "horizon": "Later", "status": "Open", "related_project": "Quotes rollout"}],
  "maintenance": [{"name": "Rotate the connector client secret", "cadence": "Annual"}],
  "captures": [{"capture": "Idea: weekly digest", "type": "Idea"}]
}
```

`session.summary` is required; everything else is optional. The payload is checked before anything is written:
- **Refused** (nothing is written): a value of the wrong type, an array that is not a list of objects or has more
  than 200 items, an item without its identifying field (`name` for projects, future needs and maintenance, `title`
  for notes, `task` for tasks, `capture` for captures), and a `session.date` that is not `YYYY-MM-DD`.
- **Reported** in `ignored_fields`: keys the wiki does not use (a misspelled `bdy`), so nothing disappears silently.
  `repos` and `session.store_raw_json` are part of the canonical JSON and accepted.

The ingest is **all or nothing**: pages are staged and written only if every page succeeds, so a late failure (for
example a note over the 200 KB page limit) leaves the silo unchanged. The staged pages are visible only to the
ingesting call: a concurrent reader sees the silo as it was until the ingest commits.

## What the server does with it
| Payload key | Result |
|---|---|
| `session` | A new page `sessions/<date>-<concerning>` (a second session that day gets `-2`). Its `scope` comes from the project it concerns. |
| `projects` | A new `projects/<slug>` page for each name not already present (case-insensitive), with its `area` saved in the frontmatter (it decides which area hub lists the project). Existing projects are not touched. |
| `notes` | A new `notes/<slug>` page, or an `## Update <date>` section appended to the existing note with that title. A different title that happens to share the slug ("C++" and "C#") gets its own page (`notes/c-2`), and a later session with the same title finds and updates that page rather than creating `notes/c-3`. |
| `tasks` | A record `tasks/<slug>` per task, deduped by title: an existing task keeps its values and only gains missing fields (plus an update section for `notes`). |
| `future_needs` | A record `future-needs/<slug>`, deduped the same way. |
| `maintenance`, `captures` | Listed as follow-ups on the session page. |

Select values are validated against `mcp_server/records.py` `ALLOWED`:

| Field | Allowed |
|---|---|
| task `status` | Not started, In progress, On Hold, Done |
| task `priority` | High, Med, Low |
| task `type` (stored as `kind`) | Personal, Business, Research |
| future need `horizon` | Now, Next, Later, Someday |
| future need `status` | Open, Promoted to Task, Dropped |
| project `type` | `Personal` gives scope `personal`; anything else gives `work` |

An unknown value is dropped and reported in `dropped_values`, never invented.

## The reply, and what to do next
```json
{"silo": "users/<you>", "session": "sessions/2026-11-01-quotes-rollout",
 "created": ["..."], "updated": ["..."], "follow_ups": {"tasks": 1, "future_needs": 1, "maintenance": 1, "captures": 1},
 "scrubbed": {}, "dropped_values": [], "ignored_fields": [], "pages_to_review": [{"path": "...", "title": "...", "because": "..."}]}
```
`scrubbed` totals what the scrubber removed across every page this ingest wrote. `pages_to_review` lists existing pages this session probably changes. Read each one and update it (`wiki_append`, or
`wiki_write` with `expected_revision`), and say so when the new information contradicts an older page. This step is
what turns a pile of session logs into a maintained wiki.

## Naming convention
Title sessions `Daily chat on YYYY-MM-DD concerning <primary project>` so the timeline reads cleanly and the same
project's sessions sort together.
