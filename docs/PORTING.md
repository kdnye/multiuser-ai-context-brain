# Porting engine changes to a fork

This repo is the canonical engine. A fork (a private deployment) carries copies of the engine files listed in
`tools/engine_files.txt`. Forks that are not tracked drift: fixes land in one place and not the other.
`python -m tools.port_check` makes that drift visible, and this page is the checklist for closing it.

## The rule
- Engine changes land in this repo first, with tests, through a pull request. Then they are ported to each fork.
- Never fix the engine only in a fork. If a fork needs a change, propose it here; if it truly cannot wait, make the
  file `diverged` in the fork's manifest with a reason, and send the change here afterwards.
- A fork's manifest (`engine-sync.json`, in the fork) says, for every engine file, whether the fork matches the engine
  at a known commit (`base_commit`), differs on purpose, owns the file, or does not have it.

## The four statuses
| Status | Meaning | Check |
|---|---|---|
| `synced` | Equal to this repo's file at `base_commit` (line endings ignored) | Edited locally: FAIL. Changed here since `base_commit`: FAIL "NEEDS PORT" |
| `diverged` | Different on purpose; the manifest holds the sha256 of the fork's file and a reason | Fork file changed: FAIL "unreviewed local change". Changed here since `base_commit`: FAIL "NEEDS PORT (diverged...)" |
| `owned` | A policy file the fork owns for good, for example its identity gate, silo names or scopes | Never compared; reported NOT COVERED with the reason |
| `absent` | The fork deliberately does not have the file | Present: FAIL "expected absent" |

Verdicts are PASS, FAIL, NOT COVERED or COULD NOT RUN. Nothing is PASS unless a comparison actually succeeded.
Exit code: 0 no FAIL, 1 any FAIL, 2 COULD NOT RUN.

## Port a change to a fork, step by step
Run from a checkout of this repo, with the fork checked out beside it.
1. Update this repo (`git pull`) so its HEAD holds the changes you want to port.
2. Read the report: `python -m tools.port_check --brain <fork>`. Every `NEEDS PORT` file needs work.
3. For each such file, read what changed here: `git diff <base_commit>..HEAD -- <file>` (the base commit is in the
   fork's `engine-sync.json`). For a `diverged` file, read the diff with the fork's reason in mind.
4. In the fork, create a branch. Apply the change by hand, or from this repo save the diff and run
   `git apply --3way <patch>` in the fork. Keep deliberate differences; take everything else.
5. In the fork, run its full tests, `ruff check .`, and its release and secret checks. Fix what breaks.
6. For each `diverged` file you re-ported, re-pin it with a real reason:
   `python -m tools.port_check --brain <fork> --accept <file> --reason "<why it still differs>"`.
7. Record the new base: `python -m tools.port_check --brain <fork> --set-base <commit> --reason "<what you ported>"`.
   It refuses unless every `synced` file now equals this repo at that commit and every `diverged` file matches its pin.
8. Run `python -m tools.port_check --brain <fork>` again. It should exit 0. Commit `engine-sync.json` with the change
   and open a pull request in the fork.

## What may diverge, and how to document it
- **Owned policy files**: the identity gate, the silo and role names, scopes, anything that names the fork's own
  organisation. Mark them `owned` with a reason. They are never compared, so review them by hand when the engine's
  interface to them changes.
- **Deliberate code differences**: mark `diverged` with the real reason ("keeps the old export format for tool X").
  Every upstream change to that file then shows as NEEDS PORT, which is the point.
- **Files the fork does not use**: mark `absent` with the reason.
- The manifest is data, not a dumping ground: a reason such as "legacy" is not a reason.

## Start tracking a fork
1. `python -m tools.port_check --brain <fork> --init`. It sets `base_commit` to this repo's HEAD, marks files equal to
   HEAD as `synced`, missing files as `absent`, and every other file as `diverged` with a placeholder reason.
2. This is a baseline, not a review. For each `diverged` file, read `diff` against this repo, decide whether the
   difference is deliberate, and then either port the fix (and make the file `synced` by editing the manifest status
   after the fork file matches HEAD), mark it `owned`, or replace the placeholder reason with the real one and run
   `--accept`.
3. Commit `engine-sync.json` in the fork. Re-run the check after every engine change here.
Use `--manifest FILE` if the manifest should live somewhere other than `<fork>/engine-sync.json`.

## When a fork wants to change a synced file
Propose it here first (an issue or pull request) so every fork gets it. If it cannot wait, edit the fork file, change
its entry to `diverged` with the sha256 and a reason, and open the matching pull request here soon after. An edit to a
`synced` file with no manifest change always fails the check.

## Common failures
| Output | What it means | What to do |
|---|---|---|
| `FAIL UNCLASSIFIED` | A file in the engine list is not in the manifest (a new engine file) | Port it or add it as `absent`/`owned` |
| `FAIL synced file edited locally` | Fork file differs from the engine at `base_commit` | Revert it, or make it `diverged` with a reason |
| `FAIL NEEDS PORT (n changed lines)` | The engine changed after `base_commit` | Port it (steps above) |
| `FAIL NEEDS PORT (diverged...)` | Same, for a file the fork changed on purpose | Read the diff, port what applies, `--accept` |
| `FAIL unreviewed local change` | A diverged file changed since it was pinned | Review the change; `--accept` with the reason, or revert |
| `FAIL expected absent` | The fork gained a file it said it would not have | Remove it, or change its status |
| `FAIL removed upstream` | The manifest lists a file this repo no longer has | Delete the file in the fork and its manifest entry |
| `NOT COVERED` | `owned`: never compared | Review by hand |
| exit 2, `COULD NOT RUN` | Upstream is not a git checkout, `base_commit` is not in it (update your checkout), the manifest is unreadable, or the fork folder is missing | Fix the cause; nothing was written |
