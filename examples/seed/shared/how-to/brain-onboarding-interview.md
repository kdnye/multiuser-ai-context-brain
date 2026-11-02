---
title: "Brain onboarding interview (master script for the AI)"
type: "how-to"
scope: "work"
source_ids: ["seed:example-2026-11", "skills:brain-onboarding"]
owner: "it"
review_by: "2027-06-30"
audience: ["all"]
---

# Brain onboarding interview (master script for the AI)

**For the AI reading this page:** this is your script. Follow it step by step with the user. Do not summarize it
back to them or ask them to read it. It works in any assistant (Claude, ChatGPT) because it is a page in the
brain, not a client feature.

**For people:** this is the one place the "How do you want your brain to work for you?" interview lives.
To run it, tell your assistant "run my brain onboarding" (or "redo onboarding"). It takes about 5 minutes and
saves a short profile to your own private area after you confirm it.

## When to run this

- First session for this user, or `reference/profile` does not exist in their own area (`me`).
- The user says: "onboarding", "set up my brain", "how should my brain work for me", "redo onboarding",
  "show my brain settings" or "change how you answer me".

## Ground rules

- One question at a time. Offer the choices. Accept a short answer.
- Save nothing the user has not confirmed. The profile goes only in their own area, never `shared`.
- Never record credentials, contact details, ID numbers, health, HR or pay data, or other people's personal
  details. Link to the system of record instead.

## Step 0: check

`wiki_read("reference/profile")` (own area) and check its frontmatter `type`.

- Exists with `type: "profile"` and the user did not ask to redo it: show it and ask "anything to change?"
  instead of re-interviewing.
- Exists with any other `type`: it is not a profile yet. The server reports `onboarded: false` for it and will
  keep doing so until the type is `profile`. Show the user what is there, offer to turn it into their profile,
  ask only for what is missing from the ten questions in Step 1, and save it in Step 3 with `type: "profile"`.
- Does not exist: run the interview.

## Step 1: the interview (10 questions)

1. **Language:** English, Español, or reply in whatever language I write in? Keep your organisation's
   jargon and acronyms in English? Ask this first, in both languages, then run the rest in their choice.
2. **Your work:** what is your role, and what fills most of your day?
3. **Time-sinks:** up to three things that eat your time.
4. **Tone:** show these three and ask which:
   - *Flat:* "Order 4471: shipped 14:20. Invoice sent."
   - *Neutral:* "Order 4471 shipped at 2:20 pm and the invoice has been sent."
   - *Personable:* "Good news - 4471 went out at 2:20 and the invoice is already sent. Anything else on that account?"
5. **Length:** for a quick question: one line / short answer plus why / the full explanation.
6. **Format:** bullets / table / short prose / numbered steps (show a tiny example of each).
7. **When should I come to you unprompted?** never / a daily digest / only on exceptions (late order, past-due
   invoice, expiring document) / suggest as you go. Then: which triggers, and where (chat, email, digest)?
8. **When I'm not sure:** always label it / only when confidence is low / never. (The shared area always labels.)
9. **If I can't help or it's risky:** tell you to check the system / name a person or role / open a ticket.
10. **Never save about you:** personal life, health, pay, other people's information, or anything else (free text).

Optional, only if relevant: accessibility (short sentences, fewer acronyms, no tables for screen readers,
read-aloud), and where they will use it (desk, phone, shop floor, on the road).

Role add-ons: read their role portal (`roles/<role>` in the `shared` silo) and ask 2-3 questions about the apps
and dashboards it lists that they actually use.

## Step 2: how your AI already knows you

Ask: "Your AI may already remember things about how you work. Want me to draft that too, for you to review?"
If yes:

- From your OWN memory, custom instructions and project instructions about this user (nothing else), fill these
  categories: role and work; communication style; working rules; tools they use; recurring tasks; things they
  told the AI not to do. Cite where each item came from (saved memory, custom instruction, a pattern you
  noticed) and label inference `[Inference]`.
- Drop anything on the never-save list from question 10, and any health, HR, pay, credentials, contact details
  or other people's personal details. Personal-life items do not cross into this work system.
- Show the list. The user approves item by item. Approved items go in the profile's **Learned** section; where
  they disagree with the interview answers, the interview wins.
- If you have no memory of this user, say so and skip this step.

## Step 3: read back and save

Show the full profile and ask "Is this right? Say yes to save it, or tell me what to change." Save only after an
explicit yes; if they ask for changes, apply them and ask again. Then:
`wiki_write("reference/profile", <page>, "onboarding: profile")` in the user's own area. If Step 0 found a page
at that path (a profile, or one with another type), pass its `revision` as `expected_revision`; on
`revision_conflict`, re-read, merge the user's new answers into the latest version, show it, and confirm again.
After the write, `wiki_read("reference/profile")` and confirm it has `type: "profile"`. Page frontmatter:

```
---
title: "Profile"
type: "profile"
scope: "work"
language: "en"
tone: "neutral"
verbosity: "short"
format: "bullets"
proactivity: "exceptions"
triggers: []
confidence_labels: "always"
escalate_to: ""
never_store: []
source_ids: ["onboarding:YYYY-MM-DD"]
---
```

Allowed values (keep frontmatter values plain; a `# comment` after a value is stored as part of the value):
language en, es or mirror; tone flat, neutral or personable; verbosity one-line, short or full; format bullets,
table, prose or steps; proactivity never, digest, exceptions or suggest.

Page body:

```
# Profile

## Stated (only the user changes this)
<their answers, one line each>

## Learned (proposed by the AI; applies only after the user approved it)
<approved items from step 2, each with its source>
```

## Later

- "Show my brain settings": read and show the profile.
- "Change X": update that one field with `wiki_write`, keeping everything else (pass the page's `revision`).
- "Redo onboarding": run steps 1-3 again.
- About two weeks after onboarding, offer once: "Is this working? More or less of anything?"

## Worked example: "a direct, technical user" (anonymized)

Tone neutral, no greetings or filler; verbosity short; bullets, tables for comparisons; decisions come as options
with trade-offs, then a recommendation; confidence labels always, at the start of the sentence ([Unverified],
[Inference]); never present generated content as fact, ask instead of guessing; proactivity on exceptions, and
answer "status?" first, before doing anything else; never change their own wording unless asked.
