"""The shared onboarding script and the server must agree on what "onboarded" means.

wiki_context reports `onboarded: true` only for reference/profile with `type: profile`. If the script treated
any existing page at that path as a finished profile, a mistyped page would be shown, never fixed, and the server
would keep saying onboarded=false forever (Codex review of the onboarded-flag PR).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "examples/seed/shared/how-to/brain-onboarding-interview.md").read_text(encoding="utf-8")
SKILL = (ROOT / "skills/brain-onboarding/SKILL.md").read_text(encoding="utf-8")


def _step(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = text.find("\n## ", start + 1)
    return text[start:] if nxt == -1 else text[start:nxt]


def test_step_zero_checks_the_type_not_just_existence():
    step0 = _step(SCRIPT, "## Step 0")
    assert 'check its frontmatter `type`' in step0
    assert 'Exists with `type: "profile"`' in step0
    assert "Exists with any other `type`" in step0
    assert "`onboarded: false`" in step0            # tells the assistant why the server still says not onboarded
    assert 'save it in Step 3 with `type: "profile"`' in step0
    assert "Does not exist" in step0


def test_step_three_overwrites_a_mistyped_page_safely_and_reads_it_back():
    step3 = _step(SCRIPT, "## Step 3")
    assert "a profile, or one with another type" in step3      # the revision guard covers the mistyped page too
    assert "`expected_revision`" in step3
    assert 'confirm it has `type: "profile"`' in step3


def test_skill_trigger_matches_the_server_flag():
    assert "`onboarded: false`" in SKILL.split("---")[1]       # frontmatter description
