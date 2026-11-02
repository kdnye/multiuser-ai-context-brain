"""The `label: value` credential rule must mask secrets, not documentation about where a secret lives.

Found by a read-only scan of the live brains: placeholders such as `<delta_link>`, references such as `$API_KEY` and
identifiers such as `from_secret_manager` were being masked on pages that document configuration.
"""

from __future__ import annotations

import pytest

from tools.wiki.scrub import scrub

LEFT_ALONE = [
    "api_key: from_secret_manager",
    "api key: stored_in_vault",
    "token: <delta_link_value>",
    "password: $DB_PASSWORD",
    "password: ${DB_PASSWORD}",
    "password: %DB_PASSWORD%",
    "client_secret: [redacted]",
    "secret = (set in the console)",
    "password: required",
    "the access_token: refresh_before_expiry is rotated hourly",
]

MASKED = [
    "password=" + "Hunter2" + "xyz",
    "api_key: " + "abcdef" + "123456",
    "client_secret: " + "s3cr3t" + "value",
    "token: " + "AbCdEfGhIjKlMnOpQrSt",
    "Password : '" + "Winter2026" + "!'",
]


@pytest.mark.parametrize("text", LEFT_ALONE)
def test_documentation_about_a_secret_is_not_masked(text):
    out, counts = scrub(f"see the note: {text} end")
    assert out == f"see the note: {text} end" and not counts["secret"]


@pytest.mark.parametrize("text", MASKED)
def test_a_labelled_secret_value_is_still_masked(text):
    out, counts = scrub(f"see the note: {text} end")
    assert counts["secret"] == 1 and "{secret}" in out
    assert "Hunter2" not in out and "123456" not in out and "s3cr3t" not in out and "AbCdEf" not in out


def test_the_label_stays_and_only_the_value_is_masked():
    out, _ = scrub("password=" + "Hunter2" + "xyz")
    assert out == "password={secret}"
