---
description: Answer a question from the brain, with citations
argument-hint: <question>
---

Answer the question using only the brain, following CLAUDE.md. Call `wiki_context` first (it searches every silo you can read), `wiki_read` only the pages that matter, and answer with citations as `silo:path` (for example `shared:how-to/brain-onboarding-interview`). If the brain does not cover it, say so plainly and do not guess. If the answer is worth keeping, offer to file it with `/save`.

Question: $ARGUMENTS
