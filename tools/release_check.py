"""Public-release gate: fail if a tracked file carries something that must not be published.

    python -m tools.release_check                      # scan the repo
    python -m tools.release_check --hash "Acme Corp"   # print the digest line for tools/release_denylist.sha256

Every file `git ls-files -co --exclude-standard` lists (tracked plus new, not ignored) is checked for:

1. **Local denylist** (`.release-denylist`, git-ignored, plain text, one term per line): your organisation's name,
   domains, cloud project ids, people's names. Case- and accent-insensitive; whole tokens or word runs (and substrings
   for terms of 8+ characters), also across line breaks.
   This is the recommended place for private terms: it never leaves your machine.
2. **Hashed denylist** (`tools/release_denylist.sha256`, committed): SHA-256 of lowercase terms of one to three
   words, matched against every token, its domain suffixes, every run of two or three words (spaced and joined), also
   across line breaks. **Warning:** an unsalted hash of a short or guessable term (an abbreviation, a domain, a
   12-digit project number, a common name) can be reversed by trying candidates, so a public hash list leaks those
   terms. Put only long, unguessable phrases here, or keep everything in the local denylist.
3. **Generic leaks**, no list needed: email addresses on real domains (anything but RFC 2606 example domains,
   `*.test`, `*.example`, `*.invalid`, `localhost`); GUIDs that do not look synthetic; Cloud Run hostnames
   (`*.run.app`); `gs://` bucket names; numbers of ten or more digits; North American phone numbers whose area code
   is not 555; and files that are not UTF-8 text (they cannot be checked, so they are reported for review).

Exit 1 with one line per hit, or print `release check: ok`. Runs in the test suite (tests/test_release_check.py).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HASHES = "tools/release_denylist.sha256"
LOCAL = ".release-denylist"
SKIP = {HASHES, LOCAL, "tools/release_check.py", "tests/test_release_check.py"}
WORD = re.compile(r"[a-z0-9][a-z0-9._@-]*[a-z0-9]|[a-z0-9]")
ALNUM = re.compile(r"[a-z0-9]+")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@([A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+)")
SAFE_DOMAIN = re.compile(r"(?:.*\.)?(?:example\.(?:com|net|org)|[a-z0-9-]+\.(?:test|example|invalid)|localhost)", re.IGNORECASE)
GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
RUN_APP = re.compile(r"(?<![*.\w-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.run\.app\b")
GS = re.compile(r"gs://[a-z0-9][a-z0-9._-]+")
LONG_NUMBER = re.compile(r"(?<![\w.-])\d{10,}(?![\w.-])")
PHONE = re.compile(r"(?<!\d)\(?(\d{3})\)?[-. ]\d{3}[-. ]\d{4}(?!\d)")


def fold(text: str) -> str:
    """Lowercase and strip accents, so 'Acmé' matches 'acme'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def digest(term: str) -> str:
    """Spaces are dropped before hashing, so 'Acme Corp', 'acme  corp' and 'acmecorp' share one digest."""
    return hashlib.sha256("".join(fold(term).split()).encode()).hexdigest()


def _entries(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.split("#", 1)[0].strip() if path.name.endswith(".sha256") else ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln)
    return out


def load_hashes(root: Path) -> set[str]:
    return set(_entries(root / HASHES))


def load_local(root: Path) -> list[str]:
    return [fold(t) for t in _entries(root / LOCAL)]


def candidates(text: str) -> set[str]:
    """Every token, its domain suffixes, its alphanumeric pieces, and every 2- and 3-word run of those pieces, both
    space-separated (local terms) and joined (hashes ignore spaces)."""
    low = fold(text)
    out = set(WORD.findall(low))
    for tok in list(out):                     # wiki.acme.internal also checks acme.internal; a@acme.com checks acme.com
        parts = tok.rsplit("@", 1)[-1].split(".")
        out.update(".".join(parts[i:]) for i in range(len(parts) - 1))
    words = ALNUM.findall(low)
    out.update(words)
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            run = words[i:i + n]
            out.add(" ".join(run))
            out.add("".join(run))
    return out


def _local_hit(term: str, low: str, cands: set[str]) -> bool:
    """A local term matches a whole token or word run (so 'abc' does not fire inside 'abcdef'); a term of eight or
    more characters also matches as a substring."""
    t = " ".join(term.split())
    return t in cands or (len(t) >= 8 and t in low)


def synthetic_guid(g: str) -> bool:
    """Test GUIDs are mostly zeros (a0000000-0000-4000-8000-000000000001) or runs of one digit per group
    (11111111-2222-3333-4444-555555555555). Real ones are neither."""
    hexes = g.lower().replace("-", "")
    return hexes.count("0") >= 16 or all(len(set(part)) == 1 for part in g.lower().split("-"))


def generic(line: str) -> list[str]:
    hits = [f"email on a real domain ({m.group(1)})" for m in EMAIL.finditer(line) if not SAFE_DOMAIN.fullmatch(m.group(1))]
    hits += [f"non-synthetic GUID {m.group(0)}" for m in GUID.finditer(line) if not synthetic_guid(m.group(0))]
    hits += [f"Cloud Run hostname {m.group(0)}" for m in RUN_APP.finditer(line)
             if not re.search(r"(?:^|[.-])example(?:[.-]|$)", m.group(0), re.IGNORECASE)]
    hits += [f"bucket name {m.group(0)}" for m in GS.finditer(line)]
    hits += [f"long number {m.group(0)}" for m in LONG_NUMBER.finditer(line) if len(set(m.group(0))) > 1]
    hits += [f"phone number {m.group(0)}" for m in PHONE.finditer(line) if m.group(1) != "555"]
    return hits


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root, capture_output=True, text=True,
                             check=True).stdout
        return [root / line for line in out.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]


def scan(root: Path) -> list[tuple[str, int, str]]:
    hashes, local, hits = load_hashes(root), load_local(root), []
    for f in tracked_files(root):
        rel = f.relative_to(root).as_posix()
        if rel in SKIP or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            hits.append((rel, 0, "not UTF-8 text: cannot be checked, review by hand or remove"))
            continue
        except OSError:
            continue
        found_hash, found_local = False, set()
        for n, line in enumerate(text.splitlines(), 1):
            low, cands = fold(line), candidates(line)
            if hashes and any(digest(c) in hashes for c in cands):
                hits.append((rel, n, "hashed denylist term"))
                found_hash = True
            for w in local:
                if _local_hit(w, low, cands):
                    hits.append((rel, n, f"local denylist term {w!r}"))
                    found_local.add(w)
            hits += [(rel, n, h) for h in generic(line)]
        flat = " ".join(fold(text).split())               # terms wrapped across a line break
        flat_cands = candidates(flat)
        if hashes and not found_hash and any(digest(c) in hashes for c in flat_cands):
            hits.append((rel, 0, "hashed denylist term (across a line break)"))
        hits += [(rel, 0, f"local denylist term {w!r} (across a line break)") for w in local
                 if w not in found_local and _local_hit(w, flat, flat_cands)]
    return hits


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if argv[:1] == ["--hash"]:
        for term in argv[1:]:
            print(digest(term))
        return 0
    hits = scan(ROOT)
    for rel, n, what in hits:
        print(f"{rel}:{n}: {what}")
    print("release check:", "FAIL" if hits else "ok")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
