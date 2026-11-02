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
# "-" also as en/em dash, and up to two separator characters ("555  123  4567"); "(555)123-4567" needs none after ")".
_PHONE = re.compile(r"(?<![\d.])(?:\+?1[\s.\-–—]?)?(?:\(\d{3}\)[\s.\-–—]{0,2}|\d{3}[\s.\-–—]{1,2})\d{3}[\s.\-–—]{1,2}\d{4}(?![\d])")
# 123-45-6789, and the same with one repeated space or dot ("123 45 6789", "123.45.6789"); a bare nine digits only
# when labelled, since an unlabelled nine-digit number is as likely an order or tracking number.
_SSN = re.compile(r"(?<!\d)\d{3}([-\s.])\d{2}\1\d{4}(?!\d)")
_SSN_LABELLED = re.compile(r"(?i)\b(ssn|social\s+security(?:\s+(?:number|no\.?|#))?)\b([^\d]{0,15}?)(\d{9})(?!\d)")
# Credentials: well-known token prefixes, PEM private keys, bearer tokens and "password = value" style assignments.
_TOKEN = re.compile(r"\b(?:(?:AKIA|ASIA)[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
                    r"|ya29\.[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9_\-]{20,})(?![A-Za-z0-9_])")
_PEM = re.compile(r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----(?:\r?\n[A-Za-z0-9+/=]{16,100}){0,80}")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{20,}")
_SECRET_LABEL = re.compile(r"(?i)\b(pass(?:word|wd)?|pwd|secret|api[_\- ]?key|access[_\- ]?key|client[_\- ]?secret|auth[_\- ]?token|token)\b(\s{0,3}[:=]\s{0,3}[\"']?)([^\s\"',;]{6,100})")
_ZERO_WIDTH = re.compile("[\u200b‌⁠﻿]")
_FULLWIDTH = re.compile("[！-～]")
_LABELLED = re.compile(r"(?i)\b(routing|account|acct|aba|member|group|policy|subscriber|claim|mrn|patient|chart)\b([^\n\d]{0,30}?)(\d[\d \-]{6,}\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A labelled birth date; the label stays, the date goes. Other dates are kept.
_DOB = re.compile(r"(?i)\b(date\s+of\s+birth|birth\s*date|birthday|born(?:\s+on)?|d\.?o\.?b\.?)\b([^\d]{0,15}?)"
                  r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4})")

# Shared mailboxes are operational knowledge, not personal contact details.
ROLE_LOCALPARTS = frozenset({
    "accounting", "admin", "administrator", "abuse", "billing", "claims", "comms", "dispatch", "helpdesk", "hello", "hr",
    "info", "it", "no-reply", "noreply", "operations", "ops", "orders", "postmaster", "quotes", "reception",
    "sales", "security", "service", "services", "support", "team", "webmaster", "dmarc", "reports",
})


def _looks_like_secret(value: str) -> bool:
    """Is the value after a `password:` / `api_key=` style label a secret, or documentation about one?

    A value that starts with `<`, `{`, `$`, `%`, `[` or `(` is a placeholder or a reference to where the secret lives
    (`<delta_link>`, `$API_KEY`), and an identifier made of letters and underscores (`from_secret_manager`) is a name,
    not a value: masking those mangled documentation. A secret is a value with a digit in it, or a long mixed-case
    string. This is a backstop: a long all-lowercase passphrase is not caught."""
    if not value or value[0] in "<{$%[(":
        return False
    if re.search(r"\d", value):
        return True
    return len(value) >= 20 and value != value.lower() and value != value.upper()


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

    def secret_label(m):
        value = m.group(3)
        if not _looks_like_secret(value):       # prose ("password: required"), identifiers and placeholders stay
            return m.group(0)
        counts["secret"] += 1
        return f"{m.group(1)}{m.group(2)}{{secret}}"

    # Invisible characters and full-width forms (an "＠" or a zero-width space inside a number) must not hide a value.
    text = _ZERO_WIDTH.sub("", text)
    text = _FULLWIDTH.sub(lambda m: chr(ord(m.group(0)) - 0xFEE0), text)
    text = _PEM.sub(simple("secret", "{secret}"), text)
    text = _TOKEN.sub(simple("secret", "{secret}"), text)
    text = _BEARER.sub(simple("secret", "Bearer {secret}"), text)
    text = _SECRET_LABEL.sub(secret_label, text)
    text = _EMAIL.sub(email, text)
    text = _SSN.sub(simple("ssn", "{ssn}"), text)
    text = _SSN_LABELLED.sub(lambda m: (counts.update(["ssn"]), f"{m.group(1)}{m.group(2)}{{ssn}}")[1], text)
    text = _DOB.sub(lambda m: (counts.update(["dob"]), f"{m.group(1)}{m.group(2)}{{dob}}")[1], text)
    text = _CARD.sub(card, text)
    text = _LABELLED.sub(labelled, text)
    if not keep_phones:
        text = _PHONE.sub(simple("phone", "{phone}"), text)
    return text, counts
