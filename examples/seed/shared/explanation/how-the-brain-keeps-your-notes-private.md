---
title: "How the brain keeps your notes private"
type: "explanation"
scope: "work"
source_ids: ["seed:example-2026-11"]
owner: "it"
review_by: "2027-06-30"
audience: ["all"]
---

# How the brain keeps your notes private

The brain keeps each person's notes in a separate area and checks every request against your sign-in. The areas
are called silos; the rules are in [[reference/brain-silos-and-who-can-read-them|the silo reference]].

- **Your own area**: through the brain, only you can read or change it. Another person's assistant cannot open it,
  and asking for it gets the same refusal as asking for an area that does not exist.
- **The shared area**: everyone signed in can read it. Only the shared-editor role can change it; an editor reviews
  a page before publishing it, as a working rule rather than something the server enforces.
- **Role areas** (finance, HR, operations): readable only with the matching role in the identity provider, so access
  is managed where accounts are already managed.

What this does not cover: whoever administers the storage behind the brain can technically reach any area outside
the brain (for example to restore a backup). The brain's access log records requests made through the connector,
not that kind of maintenance access. Never put passwords, pay, health or HR details in the brain, even in your own
area.

Why it is built this way: with one shared index, a search could show a page's title to someone who should not see
it. With separate silos, a page you cannot read never appears in your searches at all. Each request through the
brain is logged (who, which area, allowed or refused), without the page content.
