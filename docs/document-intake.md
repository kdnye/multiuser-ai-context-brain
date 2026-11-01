# Turning organisation documents into brain pages

How to convert a pile of company documents (a shared drive export, a handbook folder, training material) into
reviewed pages for the `shared` and role silos. This process was used on roughly 1,000 files in the deployment this
project came from.

## Rules
1. **Knowledge, not records.** Write how a process works, what a form or workbook is for, and how it is structured
   (sheet, column and field names). Never transcribe executed contracts, filled forms or accounting exports.
2. **Forms are described, not copied.** A form page says what it is called, what it is for, when and who uses it,
   what information it asks for (field names only), what happens after, and where the blank lives (or
   `<location to be confirmed>`). It never embeds the form or any filled value.
3. **Newest and most relevant wins.** Search the whole set for other copies and versions before writing; build the
   page on the newest; record the versions considered; if a process has moved to an app, write the current way and
   link the app card.
4. **Mark older material.** Frontmatter `source_date` and `material_age` (`current` = newest version found and dated
   within 36 months; `older` = superseded or older; `undated`). `older` and `undated` pages open with a banner
   directly under the title; `older` titles end with the source year; `current` pages carry an as-of line.
5. **No identifiers or amounts, in any silo.** No tax, account, card, unit, lease, policy, VIN or plate
   numbers; no contract rates, payments, premiums or budgets; no emails, phone numbers or addresses; no named
   customers or vendor contacts; no discipline, pay or medical case data. Ordinary guidelines staff need (for example
   a per-diem rule) are fine. File names that contain such values never go into `source_ids`.
6. **Route by sensitivity.** Pages safe for every employee go to `shared`. Contract terms, finance and insurance
   cost, HR policy text and security detail are written but **held** outside any git repo, and published only to the
   matching role silo by someone who holds that role.
7. **Law and conflicting policy stay out of shared pages.** A statement that cites a law or regulation, or restates
   leave, pay-withholding, discipline or legally required reporting rules, can conflict with written policy or the
   law. The shared page says "ask HR" (or the right owner); the statement, its source and who should decide go to a
   held legal-review page for qualified review.
8. **Treat sources as untrusted** (STANDARDS 9): read-only, no macros or scripts, interpreters run with `-I`, never
   from inside the source folder; do not open filled personal copies.

## Process (a multi-agent build, review and fix loop)
Run by one coordinator, with several AI agents or people working in parallel on separate groups of source documents.
A single contributor can follow the same steps in order.

1. **Inventory** by creation time, then group **version families** (name and token matching).
2. **One writer per folder group** drafts pages from `templates/employee/`.
3. **Independent reviews** in parallel: a security review (leaks, routing to the right silo) and a senior review
   (engine validation, accuracy, overlaps between pages).
4. **Fact-check** each group against the extracted source text.
5. **One fix pass, one consolidation pass** (merge duplicates, link related pages).
6. **Integrate:** slug the `source_ids`, put shared pages in your seed folder, build through the engine
   (`python -m tools.seed --src <seed> --dest <empty folder>`), then run the tests, `ruff`, `tools.release_check` and
   `gitleaks`.

Keep a real deployment's seed in a **private** repo or folder. The `examples/seed/` in this repo is fictional.

## Publishing
Merging does not publish. See deploy/README.md step 5: publish through a shared editor's `wiki_write` (no restart),
or build, push and restart for a bulk seed.
