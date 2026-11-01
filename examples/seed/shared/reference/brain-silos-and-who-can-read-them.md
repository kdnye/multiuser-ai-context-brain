---
title: "Brain silos and who can read them"
type: "reference"
scope: "work"
source_ids: ["seed:example-2026-11", "code:mcp_server/access.py"]
owner: "it"
review_by: "2027-06-30"
audience: ["all"]
---

# Brain silos and who can read them

A silo is a separate wiki with its own index, links and search. The server checks every request against your
sign-in; the assistant's prompt never decides access.

| Silo | Who reads | Who writes |
|---|---|---|
| your own (`me`) | only you | only you |
| `shared` | everyone signed in | the shared-editor role |
| `finance` | the `finance` role | the `finance` role |
| `hr` | the `hr` role | the `hr` role |
| `ops` | the `ops` role | the `ops` role |

The role silos above are this example's defaults; your deployment lists its own (`BRAIN_ROLE_SILOS`). Roles are
assigned in the identity provider (for example Microsoft Entra app roles), so access changes happen there.

- Asking for a silo you cannot read gets the same refusal as asking for one that does not exist.
- Search and recall run only over the silos you can read, and every result names its silo.
- Sessions you save always land in your own silo.

Why it works this way: [[explanation/how-the-brain-keeps-your-notes-private|How the brain keeps your notes private]].
