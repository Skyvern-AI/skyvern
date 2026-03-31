from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.skill_test_helpers import first_nonempty_line_after_h1

ROOT = Path(__file__).resolve().parents[2]
BUNDLED_SMOKE_TEST_SKILL = ROOT / "skyvern" / "cli" / "skills" / "smoke-test" / "SKILL.md"
CLAUDE_SMOKE_TEST_SKILL = ROOT / ".claude" / "skills" / "smoke-test" / "SKILL.md"

_needs_cloud_repo = pytest.mark.skipif(
    not CLAUDE_SMOKE_TEST_SKILL.exists(),
    reason=".claude/skills/smoke-test/SKILL.md not present (OSS checkout)",
)


@_needs_cloud_repo
def test_bundled_and_claude_smoke_test_skill_match_exactly() -> None:
    assert BUNDLED_SMOKE_TEST_SKILL.read_text(encoding="utf-8") == CLAUDE_SMOKE_TEST_SKILL.read_text(encoding="utf-8")


def test_smoke_test_frontmatter_name_matches_folder() -> None:
    skill_text = BUNDLED_SMOKE_TEST_SKILL.read_text(encoding="utf-8")
    assert "name: smoke-test" in skill_text


def test_smoke_test_contains_browser_tool_execution_model() -> None:
    skill_text = BUNDLED_SMOKE_TEST_SKILL.read_text(encoding="utf-8")
    assert "skyvern_act" in skill_text
    assert "skyvern_validate" in skill_text


def test_smoke_test_output_table_format() -> None:
    skill_text = BUNDLED_SMOKE_TEST_SKILL.read_text(encoding="utf-8")
    assert "| Flow | Result | Evidence |" in skill_text


def test_smoke_test_pr_evidence_markers_present() -> None:
    skill_text = BUNDLED_SMOKE_TEST_SKILL.read_text(encoding="utf-8")
    assert "<!-- skyvern-smoke-test-report -->" in skill_text
    assert "gh pr comment" in skill_text
    assert ".qa/latest-smoke-report.md" in skill_text


def test_smoke_test_browser_session_lifecycle() -> None:
    skill_text = BUNDLED_SMOKE_TEST_SKILL.read_text(encoding="utf-8")
    assert "skyvern_browser_session_create" in skill_text
    assert "skyvern_browser_session_close" in skill_text


def test_smoke_test_is_diff_driven() -> None:
    skill_text = BUNDLED_SMOKE_TEST_SKILL.read_text(encoding="utf-8")
    assert "git diff" in skill_text


def test_smoke_test_under_max_lines() -> None:
    skill_text = BUNDLED_SMOKE_TEST_SKILL.read_text(encoding="utf-8")
    line_count = len(skill_text.splitlines())
    assert line_count <= 500, f"SKILL.md has {line_count} lines (max 500)"


def test_smoke_test_has_summary_line_before_note_comment() -> None:
    skill_text = BUNDLED_SMOKE_TEST_SKILL.read_text(encoding="utf-8")
    first_line = first_nonempty_line_after_h1(skill_text)
    assert first_line
    assert not first_line.startswith("<!--")


def test_smoke_test_always_include_url_isolation_rule() -> None:
    """The SKILL.md must instruct to always include the url param to prevent state bleed."""
    skill_text = BUNDLED_SMOKE_TEST_SKILL.read_text(encoding="utf-8")
    assert "Always include the `url` parameter" in skill_text


def test_readme_lists_smoke_test() -> None:
    readme = (ROOT / "skyvern" / "cli" / "skills" / "README.md").read_text(encoding="utf-8")
    assert "smoke-test" in readme


@_needs_cloud_repo
def test_validate_skills_package_script_passes_with_smoke_test() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_skills_package.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
