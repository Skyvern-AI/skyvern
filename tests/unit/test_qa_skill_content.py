from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from skyvern.cli.mcp_tools.prompts import QA_TEST_CONTENT, qa_test

ROOT = Path(__file__).resolve().parents[2]
BUNDLED_QA_SKILL = ROOT / "skyvern" / "cli" / "skills" / "qa" / "SKILL.md"
CLAUDE_QA_SKILL = ROOT / ".claude" / "skills" / "qa" / "SKILL.md"

_needs_cloud_repo = pytest.mark.skipif(
    not CLAUDE_QA_SKILL.exists(),
    reason=".claude/skills/qa/SKILL.md not present (OSS checkout)",
)


def _first_nonempty_line_after_h1(text: str) -> str:
    after_h1 = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if raw_line.startswith("# "):
            after_h1 = True
            continue
        if not after_h1 or not line:
            continue
        return line
    return ""


@_needs_cloud_repo
def test_bundled_and_claude_qa_skill_match_exactly() -> None:
    assert BUNDLED_QA_SKILL.read_text(encoding="utf-8") == CLAUDE_QA_SKILL.read_text(encoding="utf-8")


def test_qa_skill_has_summary_line_before_note_comment() -> None:
    skill_text = BUNDLED_QA_SKILL.read_text(encoding="utf-8")
    first_line_after_h1 = _first_nonempty_line_after_h1(skill_text)
    assert first_line_after_h1
    assert not first_line_after_h1.startswith("<!--")


def test_qa_skill_mentions_backend_validation_modes() -> None:
    skill_text = BUNDLED_QA_SKILL.read_text(encoding="utf-8")
    required_markers = [
        "# QA — Validate Frontend and Backend Changes",
        "Frontend/browser",
        "Backend API",
        "Backend-internal",
        "Mixed",
        "## Step 4B: Backend API QA",
        "## Step 4C: Backend-Internal QA",
        "skyvern browser serve --port 9222",
        "curl -sS",
        "If none respond, start the most direct repo-documented local command for the\nchanged surface.",
        "combined frontend/backend dev script",
        "The primary mode is still **diff-driven**.",
    ]
    for marker in required_markers:
        assert marker in skill_text


def test_qa_prompt_mentions_backend_validation_modes() -> None:
    required_markers = [
        "# QA — Validate Frontend and Backend Changes",
        "Frontend/browser",
        "Backend-internal",
        "## Step 3B: Backend API QA",
        "## Step 3C: Backend-Internal QA",
        "Start it with the most direct repo-documented local command for the changed",
        "combined frontend/backend dev script",
        "skyvern browser serve --port 9222",
        'curl -sS -H "Authorization: Bearer <token>"',
        "Default to `skyvern_evaluate` for frontend/browser assertions.",
    ]
    for marker in required_markers:
        assert marker in QA_TEST_CONTENT


def test_qa_prompt_docs_only_lightweight_rule() -> None:
    assert "If the diff is mostly documentation or comments, keep QA lightweight" in QA_TEST_CONTENT


def test_qa_prompt_mixed_mode_backend_contract_warning() -> None:
    assert "If the backend contract is broken, frontend results are not trustworthy" in QA_TEST_CONTENT


def test_qa_test_prompt_includes_target_url_and_focus_area() -> None:
    rendered = qa_test(url="http://localhost:8000", context="validate the workflow filters API")
    assert "Target URL: `http://localhost:8000`" in rendered
    assert "Focus area: validate the workflow filters API" in rendered
    assert "choose the correct validation mode" in rendered


def test_qa_pr_evidence_markers_present() -> None:
    """Assert the PR evidence posting instructions are present in all /qa surfaces."""
    skill_text = BUNDLED_QA_SKILL.read_text(encoding="utf-8")

    # Check SKILL.md
    assert "<!-- skyvern-qa-report -->" in skill_text
    assert "Post Evidence to PR" in skill_text
    assert ".qa/latest-report.md" in skill_text
    assert "gh pr comment" in skill_text

    # Check QA_TEST_CONTENT (MCP prompt)
    assert "<!-- skyvern-qa-report -->" in QA_TEST_CONTENT
    assert "Post Evidence to PR" in QA_TEST_CONTENT
    assert ".qa/latest-report.md" in QA_TEST_CONTENT
    assert "gh pr comment" in QA_TEST_CONTENT


@_needs_cloud_repo
def test_validate_skills_package_script_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_skills_package.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
