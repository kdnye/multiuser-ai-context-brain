---
name: brain-onboarding
description: First-run setup for the context brain - "How do you want your brain to work for you?". Use when a user connects the brain for the first time, when their silo has no reference/profile page, or when they say "redo onboarding", "show my brain settings" or "change how you answer me". Loads the master interview script from the brain and follows it.
---

# Brain onboarding

The interview lives in the brain, not here, so every client (Claude, ChatGPT) runs the same script:

1. `wiki_read("how-to/brain-onboarding-interview", silo="shared")`.
2. Follow that page step by step. Do not paraphrase it from memory, and do not duplicate it in this file.

If the page is missing from the shared silo, tell the user the onboarding script has not been published yet and
ask them to contact the shared-area editor. Do not improvise a different interview.

Source of truth: `examples/seed/shared/how-to/brain-onboarding-interview.md` in this repo, published to the shared
silo with `python -m tools.seed` and `python -m tools.wiki push`.
