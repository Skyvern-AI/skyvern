from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUNDLED_SMOKE_TEST_SKILL = ROOT / "skyvern" / "cli" / "skills" / "smoke-test" / "SKILL.md"


def test_smoke_test_sticky_comment_lookup_is_owned_anchored_and_paginated() -> None:
    """The lookup must not be able to PATCH a comment this user did not author (SKY-13090)."""
    skill_text = BUNDLED_SMOKE_TEST_SKILL.read_text(encoding="utf-8")
    assert 'test("skyvern-smoke-test-report")' not in skill_text
    assert "select(.user.login == $ENV.GH_LOGIN)" in skill_text
    assert 'select(.body | startswith("<!-- skyvern-smoke-test-report -->"))' in skill_text
    assert "gh api --paginate" in skill_text
