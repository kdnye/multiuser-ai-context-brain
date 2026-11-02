# Porting engine changes to a fork

This repo is the canonical engine. A fork (a private deployment) carries copies of the engine files listed in
`tools/engine_files.txt`. Forks that are not tracked drift: fixes land in one place and not the other.
`python -m tools.port_check` makes that drift visible, and this page is the checklist for closing it.

## The rule
- Engine changes land in this repo first, with tests, through a pull request. Then they are ported to each fork.
- Never fix the engine only in a fork. If a fork needs a change, propose it here; if it truly cannot wait, make the
  file `diverged` in the fork's manifest with a reason (see "When a fork wants to change a synced file"), and send the
  change here afterwards.
- A fork's manifest (`engine-sync.json`, in the fork) says, for every engine file, whether the fork matches the engine
  at a known commit (`base_commit`), differs on purpose, owns the file, or does not have it.

## What the tool reads
- **Upstream is read at its committed HEAD, through git.** A dirty working tree or index is ignored, and so is any
  branch you are not on. Before every run, check out the branch you mean (normally `main`) and `git pull`. The tool
  prints the branch and warns when it is not the default branch or HEAD is detached.
- If the upstream checkout is behind the fork's `base_commit` the tool stops (COULD NOT RUN: "behind base_commit"),
  because its advice would revert fixes. Update the checkout.
- Files are compared with CRLF turned into LF. The sha256 the manifest stores is of that normalised text, so
  `sha256sum` on the raw file gives a different value. Never compute it by hand; use `--accept`.
- Fork files and upstream contents are only read and hashed, never executed.

## The four statuses
| Status | Meaning | Check |
|---|---|---|
| `synced` | Equal to this repo's file at `base_commit` (line endings ignored) | Edited locally: FAIL. Changed here since `base_commit`: FAIL "NEEDS PORT". Already equal to HEAD: PASS "ported to HEAD; run --set-base" |
| `diverged` | Different on purpose; the manifest holds the sha256 of the fork's file, a reason and `reviewed` | Unreviewed: FAIL "baseline divergence not reviewed". Fork file changed: FAIL "unreviewed local change". `todo` set: FAIL "TODO: port" or "TODO: upstream this". Changed here since `base_commit`: FAIL "NEEDS PORT (diverged...)" |
| `owned` | A policy file the fork owns for good, for example its identity gate, silo names or scopes. May be a path this repo never had | Never compared; reported NOT COVERED with the reason |
| `absent` | The fork deliberately does not have the file | Anything at that path (file or folder): FAIL "expected absent" |

Fork files under an engine glob that this repo does not have and the manifest does not list are reported NOT COVERED
"fork-only"; list them as `owned` if they are meant to stay.

Verdicts are PASS, FAIL, NOT COVERED or COULD NOT RUN. Nothing is PASS unless a comparison actually succeeded, and the
overall result is PASS only if no row failed and at least one `synced` or `diverged` file was compared (a fork whose
files are all `owned` is NOT COVERED and exits 1). Exit codes: 0 PASS, 1 FAIL or NOT COVERED, 2 COULD NOT RUN.

## Port a change to a fork, step by step
Run from a checkout of this repo (on the branch you mean, up to date), with the fork checked out beside it.
1. Update this repo (`git pull`) so its HEAD holds the changes you want to port.
2. Read the report: `python -m tools.port_check --brain <fork>`. Every `NEEDS PORT` file needs work.
3. For each such file, read what changed here: `git diff <base_commit>..HEAD -- <file>` (the base commit is in the
   fork's `engine-sync.json`). For a `diverged` file, read the diff with the fork's reason in mind.
4. In the fork, create a branch. Apply the change by hand, or save the diff and run `git apply --3way <patch>` in the
   fork. The fork usually lacks this repo's blobs, so `--3way` falls back to a plain apply and may reject hunks:
   fix them by hand. After copying a checkout into place, run `git update-index --refresh` before applying.
5. In the fork, run its full tests, `ruff check .`, and its release and secret checks. Fix what breaks.
6. For each `diverged` file you re-ported, record the review and why it still differs:
   `python -m tools.port_check --brain <fork> --accept <file> --reason "<why it still differs>"`.
   This also records the upstream commit you reviewed it through. A `synced` file that now equals HEAD needs nothing.
7. Record the new base: `python -m tools.port_check --brain <fork> --set-base <commit> --reason "<what you ported>"`.
   `<commit>` must be a hex commit id (or `HEAD`); branch names, tags and `HEAD~1` are refused, as is a hex id that is
   also a branch or tag name. It must descend from the current base. It refuses unless every `synced` file equals this
   repo at that commit, every `diverged` file matches its pin and is reviewed, and every `diverged` file that changed here
   since the old base was reviewed through the new base (it names the files that block).
8. Run `python -m tools.port_check --brain <fork>` again. It should exit 0. Commit `engine-sync.json` with the change
   and open a pull request in the fork.

## What may diverge, and how to document it
- **Owned policy files**: the identity gate, the silo and role names, scopes, anything that names the fork's own
  organisation, and files that exist only in the fork. Mark them `owned` with a reason. They are never compared, so
  review them by hand when the engine's interface to them changes.
- **Deliberate code differences**: mark `diverged` with the real reason ("keeps the old export format for tool X").
  Every upstream change to that file then shows as NEEDS PORT, which is the point.
- **A fix the fork has and this repo lacks**: `--accept <file> --reason "..." --todo upstream` keeps the file failing as
  "TODO: upstream this" until the fix is contributed here and ported back. Clear it with `--todo none`.
- **Upstream has a change the fork has not adopted yet**: `--accept ... --todo port` fails as "TODO: port" until done.
- **Files the fork does not use**: mark `absent` with the reason.
- The manifest is data, not a dumping ground: a reason such as "legacy" is not a reason.

## Start tracking a fork
1. `python -m tools.port_check --brain <fork> --init`. It sets `base_commit` to this repo's HEAD, marks files equal to
   HEAD as `synced`, missing files as `absent`, and every other file as `diverged` with `reviewed: false` and a
   placeholder reason.
2. **A fresh baseline is expected to FAIL.** Every unreviewed divergence reports "baseline divergence not reviewed"
   until a person looks at it. That is the point: the first run shows how far the fork has drifted, and it must not
   look clean before anyone has checked.
3. For each such file, read the difference against this repo and decide:
   - the fork should adopt this repo's version: port it by hand, then edit the entry to `synced` in the manifest (or
     `--accept ... --todo port` while the work is open);
   - the fork is ahead: `--accept <file> --reason "..." --todo upstream`, then contribute the fix here;
   - the difference is deliberate: `--accept <file> --reason "<the real reason>"`;
   - the file is the fork's own policy: change the entry to `owned` with a reason.
4. Commit `engine-sync.json` in the fork. Re-run the check after every engine change here.
`--init --force` on an existing manifest keeps every existing entry, reason, `reviewed`, `todo`, the base and the
history, adds entries only for engine files not yet listed, and first saves the old file as `engine-sync.json.bak`.
Use `--manifest FILE` if the manifest should live somewhere other than `<fork>/engine-sync.json`.

## When a fork wants to change a synced file
Propose it here first (an issue or pull request) so every fork gets it. If it cannot wait, edit the fork file and run
`python -m tools.port_check --brain <fork> --accept <file> --diverge --reason "<why>"`. The tool hashes the file
itself and turns the entry into `diverged`, reviewed; do not compute a sha256 by hand. Open the matching pull request
here soon after (and use `--todo upstream` until it lands). An edit to a `synced` file with no manifest change always
fails the check.

## When this repo renames or removes a file
Do not just delete the fork's copy: a `diverged` file carries deliberate changes. Look at what happened upstream:
`git diff -M --name-status <base_commit>..HEAD` (in this repo). The tool also says "possibly renamed to X" on the old
row and "possibly renamed from X" on the new one. For a rename, move the fork file AND its manifest entry to the new
path, then re-review the file (`--accept`). Only when the file is truly gone, and the fork's changes to it are
unwanted, delete the file and its manifest entry.

## Common failures
| Output | What it means | What to do |
|---|---|---|
| `FAIL UNCLASSIFIED` | A file in the engine list is not in the manifest (a new engine file, or a rename target) | Port it or add it as `absent`/`owned` |
| `FAIL synced file edited locally` | Fork file differs from the engine at `base_commit` (the note adds "differs only by a BOM?" when that is the whole difference) | Revert it, or `--accept --diverge` with a reason |
| `PASS ported to HEAD; run --set-base` | The fork file already equals this repo's HEAD: a correct port | Run `--set-base` once all files are ported |
| `FAIL NEEDS PORT (n changed lines)` or `(binary)` | The engine changed after `base_commit` | Port it (steps above) |
| `FAIL NEEDS PORT (diverged...)` | Same, for a file the fork changed on purpose | Read the diff, port what applies, `--accept` |
| `FAIL baseline divergence not reviewed` | `--init` recorded a difference nobody has reviewed | Decide as in "Start tracking a fork", then `--accept` |
| `FAIL TODO: port` / `TODO: upstream this` | Follow-up work recorded with `--todo` is open | Do it, then `--accept ... --todo none` |
| `FAIL unreviewed local change` | A diverged file changed since it was pinned | Review the change; `--accept` with the reason, or revert |
| `FAIL expected absent` | The fork gained a file or folder it said it would not have | Remove it, or change its status |
| `FAIL removed upstream` | The manifest lists a file this repo no longer has | See "When this repo renames or removes a file" |
| `NOT COVERED` | `owned`, or "fork-only": never compared | Review by hand |
| exit 2, `COULD NOT RUN` | Upstream is not a git checkout or is behind `base_commit`; `base_commit` is not in it (shallow clone? run `git fetch --unshallow` in the upstream checkout); the manifest or engine list is unreadable; an engine list entry matches nothing; the fork folder is missing; a link leaves the fork folder | Fix the cause; nothing was written |
