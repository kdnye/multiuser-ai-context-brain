"""Remove contact details and account-style numbers before text is committed (STANDARDS rule 3).

Lossy on purpose: the originals stay in the git-ignored raw copy. Business facts such as hostnames,
project numbers, dollar amounts, dates and shared role mailboxes (accounting@, ops@ ...) are left
alone; personal email addresses, phone numbers and identity/financial numbers are replaced with a
visible marker so a reader knows something was removed.
"""

from __future__ import annotations

import re
from collections import Counter

# Every quantifier is bounded (RFC limits: local part 64, label 63), so a long run with no "@" costs at most 64 steps
# per start position: linear, not quadratic (a 128 KB run of "a" took 56 s with the unbounded form).
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,20}\.[A-Za-z]{2,24}")
_PHONE = re.compile(r"(?<![\d.])(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?![\d])")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_LABELLED = re.compile(r"(?i)\b(routing|account|acct|aba|member|group|policy|subscriber|claim|mrn|patient|chart)\b([^\n\d]{0,30}?)(\d[\d \-]{6,}\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A labelled birth date; the label stays, the date goes. Other dates are kept.
_DOB = re.compile(r"(?i)\b(date\s+of\s+birth|birth\s*date|birthday|d\.?o\.?b\.?)\b([^\n\d]{0,15}?)(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})")

# Shared mailboxes are operational knowledge, not personal contact details.
ROLE_LOCALPARTS = frozenset({
    "accounting", "admin", "administrator", "abuse", "billing", "claims", "comms", "dispatch", "helpdesk", "hello", "hr",
    "info", "it", "no-reply", "noreply", "operations", "ops", "orders", "postmaster", "quotes", "reception",
    "sales", "security", "service", "services", "support", "team", "webmaster", "dmarc", "reports",
})


def _luhn(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if flip:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        flip = not flip
    return total % 10 == 0


def scrub(text: str, keep_emails: bool = False, keep_phones: bool = False) -> tuple[str, Counter]:
    """Return (clean text, counts of what was replaced). keep_emails: person pages may keep email addresses
    so people can be matched to calendar invites and mail; phones and IDs are still removed. Off by default.
    keep_phones: only for a person page in a user's own personal silo (set by the engine, never by a caller); IDs, cards
    and dates of birth are still removed."""
    counts: Counter = Counter()

    def email(m):
        if keep_emails or m.group(0).split("@", 1)[0].lower() in ROLE_LOCALPARTS:
            return m.group(0)
        counts["email"] += 1
        return "{email}"

    def card(m):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            counts["card"] += 1
            return "{card}"
        return m.group(0)

    def labelled(m):
        number = m.group(3)
        if _DATE.fullmatch(number.strip()) or len(re.sub(r"\D", "", number)) < 7:
            return m.group(0)
        counts["labelled_number"] += 1
        return f"{m.group(1)}{m.group(2)}{{number}}"

    def simple(key, repl):
        def f(_m):
            counts[key] += 1
            return repl
        return f

    text = _EMAIL.sub(email, text)
    text = _SSN.sub(simple("ssn", "{ssn}"), text)
    text = _DOB.sub(lambda m: (counts.update(["dob"]), f"{m.group(1)}{m.group(2)}{{dob}}")[1], text)
    text = _CARD.sub(card, text)
    text = _LABELLED.sub(labelled, text)
    if not keep_phones:
        text = _PHONE.sub(simple("phone", "{phone}"), text)
    return text, counts
