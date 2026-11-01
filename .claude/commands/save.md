---
description: File the last answer or analysis back into your own silo as a page
argument-hint: <short title>
---

Turn the most recent answer, comparison or analysis into a page so it compounds. Choose `answers/<slug>` for a specific answer or `concepts/<slug>` for a reusable idea. Write it to your own silo with `wiki_write`: frontmatter (title, type `answer` or `concept`, scope, source_ids listing the pages you cited plus `claude-session:YYYY-MM-DD`), a short body, and `[[wikilinks]]` to every page it relies on. Then link it from the most relevant existing page so it is not an orphan. Writing to `shared` or a role silo needs the matching role and the user's explicit approval of the destination and content.

Title: $ARGUMENTS
