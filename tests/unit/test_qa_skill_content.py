from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from skyvern.cli import skill_commands
from skyvern.cli.core.session_manager import set_stateless_http_mode
from skyvern.cli.mcp_tools.prompts import QA_TEST_CONTENT, qa_test
from tests.unit.skill_test_helpers import first_nonempty_line_after_h1

ROOT = Path(__file__).resolve().parents[2]
BUNDLED_QA_SKILL = ROOT / "skyvern" / "cli" / "skills" / "qa" / "SKILL.md"
CLAUDE_QA_SKILL = ROOT / ".claude" / "skills" / "qa" / "SKILL.md"
CLAUDE_QA_EVIDENCE_SKILL = ROOT / ".claude" / "skills" / "qa-evidence" / "SKILL.md"

_needs_cloud_repo = pytest.mark.skipif(
    not CLAUDE_QA_SKILL.exists(),
    reason=".claude/skills/qa/SKILL.md not present (OSS checkout)",
)


@_needs_cloud_repo
def test_bundled_and_claude_qa_skill_match_exactly() -> None:
    assert BUNDLED_QA_SKILL.read_text(encoding="utf-8") == CLAUDE_QA_SKILL.read_text(encoding="utf-8")


def test_qa_skill_has_summary_line_before_note_comment() -> None:
    skill_text = BUNDLED_QA_SKILL.read_text(encoding="utf-8")
    first_line_after_h1 = first_nonempty_line_after_h1(skill_text)
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


def test_qa_test_prompt_stateless_http_omits_local_shell_and_filesystem_steps() -> None:
    set_stateless_http_mode(True)
    try:
        rendered = qa_test()
    finally:
        set_stateless_http_mode(False)

    assert ".qa/latest-report.md" not in rendered
    assert "gh pr comment" not in rendered
    assert "git diff --name-only HEAD~1" not in rendered
    assert "local shell, git,\nfilesystem, or `gh` access" in rendered
    assert "writing a local report file" in rendered


def test_qa_pr_evidence_markers_present() -> None:
    """Assert the PR evidence posting instructions are present in all /qa surfaces."""
    skill_text = BUNDLED_QA_SKILL.read_text(encoding="utf-8")

    # Check SKILL.md
    assert "<!-- skyvern-qa-report -->" in skill_text
    assert "Post Evidence to PR" in skill_text
    assert ".qa/latest-report.md" in skill_text
    assert "skyvern skill post-qa-report" in skill_text
    assert 'COMMENT_BODY="' not in skill_text
    assert "gh pr comment" not in skill_text
    assert 'gh api "repos/{owner}/{repo}/issues/' not in skill_text

    # Check QA_TEST_CONTENT (MCP prompt)
    assert "<!-- skyvern-qa-report -->" in QA_TEST_CONTENT
    assert "Post Evidence to PR" in QA_TEST_CONTENT
    assert ".qa/latest-report.md" in QA_TEST_CONTENT
    assert "skyvern skill post-qa-report" in QA_TEST_CONTENT
    assert 'COMMENT_BODY="' not in QA_TEST_CONTENT
    assert "gh pr comment" not in QA_TEST_CONTENT
    assert 'gh api "repos/{owner}/{repo}/issues/' not in QA_TEST_CONTENT


QA_MARKER = "<!-- skyvern-qa-report -->"


def _comment_line(comment_id: int, login: str, head: str) -> str:
    return json.dumps({"id": comment_id, "login": login, "head": head})


def _stage_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    """Write .qa/latest-report.md under tmp_path and make it the working directory."""
    monkeypatch.chdir(tmp_path)
    report_file = tmp_path / ".qa" / "latest-report.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(text, encoding="utf-8")
    return report_file


def _fake_gh(
    calls: list[tuple[list[str], dict[str, object]]],
    *,
    pr_number: str = "42",
    login: str = "qa-user",
    login_returncode: int = 0,
    comment_lines: str = "",
    on_shell_invocation: Callable[[], object] = lambda: None,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if kwargs.get("shell") is not False:
            on_shell_invocation()

        returncode = 0
        stdout = ""
        if command[:3] == ["git", "rev-parse", "--short"]:
            stdout = "abc123\n"
        elif command[:3] == ["gh", "pr", "status"]:
            stdout = f"{pr_number}\n"
        elif command[:3] == ["gh", "api", "user"]:
            stdout = f"{login}\n"
            returncode = login_returncode
        elif command[:2] == ["gh", "api"] and any(arg.endswith("/comments") for arg in command):
            stdout = comment_lines
        return subprocess.CompletedProcess(command, returncode=returncode, stdout=stdout, stderr="")

    return fake_run


def _lookup_command(calls: list[tuple[list[str], dict[str, object]]]) -> list[str] | None:
    return next(
        (
            command
            for command, _ in calls
            if command[:2] == ["gh", "api"] and any(a.endswith("/comments") for a in command)
        ),
        None,
    )


def _patch_commands(calls: list[tuple[list[str], dict[str, object]]]) -> list[list[str]]:
    return [command for command, _ in calls if "PATCH" in command]


@pytest.mark.parametrize(
    ("comment_lines", "expected_id"),
    [
        ("", None),
        (_comment_line(99, "qa-user", QA_MARKER), "99"),
        # Only this user's own comments are editable; a foreign comment whose body merely
        # opens with or mentions the marker must never be the PATCH target (SKY-13090).
        (_comment_line(7, "someone-else", QA_MARKER), None),
        (_comment_line(8, "qa-user", f"a review quoting {QA_MARKER} inline"), None),
        (
            "\n".join(
                [
                    _comment_line(7, "someone-else", QA_MARKER),
                    _comment_line(8, "qa-user", f"quoting {QA_MARKER}"),
                    _comment_line(99, "qa-user", QA_MARKER),
                ]
            ),
            "99",
        ),
        ("not json at all", None),
    ],
)
def test_find_sticky_comment_id_requires_own_comment_and_exact_marker(
    comment_lines: str,
    expected_id: str | None,
) -> None:
    assert skill_commands._find_sticky_comment_id(comment_lines, "qa-user") == expected_id


def test_find_sticky_comment_id_scans_past_the_first_api_page() -> None:
    """GitHub pages comments at 30; the sticky comment must still be found beyond that."""
    lines = [_comment_line(i, "someone-else", f"filler {i}") for i in range(40)]
    lines.append(_comment_line(99, "qa-user", QA_MARKER))
    assert skill_commands._find_sticky_comment_id("\n".join(lines), "qa-user") == "99"


@pytest.mark.parametrize("comment_lines", ["", _comment_line(99, "qa-user", QA_MARKER)])
def test_post_report_passes_pr_derived_content_as_literal_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    comment_lines: str,
) -> None:
    report_text = "PR-derived inert marker: ; & | $(qa-inert-marker) `qa-inert-marker`"
    _stage_report(tmp_path, monkeypatch, report_text)
    side_effect_marker = tmp_path / "shell-evaluated"
    calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(
        skill_commands.subprocess,
        "run",
        _fake_gh(
            calls,
            comment_lines=comment_lines,
            on_shell_invocation=lambda: side_effect_marker.write_text("unsafe shell invocation", encoding="utf-8"),
        ),
    )

    skill_commands.post_qa_report()

    assert calls
    assert all(isinstance(command, list) and kwargs["shell"] is False for command, kwargs in calls)
    post_call = calls[-1][0]
    if comment_lines:
        assert post_call[:3] == ["gh", "api", "repos/{owner}/{repo}/issues/comments/99"]
        body = post_call[post_call.index("-f") + 1].removeprefix("body=")
    else:
        assert post_call[:4] == ["gh", "pr", "comment", "42"]
        body = post_call[post_call.index("--body") + 1]
    assert report_text in body
    assert not side_effect_marker.exists()


def test_post_report_lookup_paginates_and_scopes_to_own_marker_comment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stage_report(tmp_path, monkeypatch, "evidence")
    calls: list[tuple[list[str], dict[str, object]]] = []
    foreign = _comment_line(5126543280, "claude[bot]", "<!-- claude-code-review -->")
    monkeypatch.setattr(skill_commands.subprocess, "run", _fake_gh(calls, comment_lines=foreign))

    skill_commands.post_qa_report()

    lookup = _lookup_command(calls)
    assert lookup is not None
    # Without --paginate the lookup stops at 30 comments and appends a duplicate instead
    # of updating the sticky comment.
    assert "--paginate" in lookup
    assert not _patch_commands(calls)
    assert calls[-1][0][:4] == ["gh", "pr", "comment", "42"]


def test_post_report_does_not_edit_any_comment_when_gh_user_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stage_report(tmp_path, monkeypatch, "evidence")
    calls: list[tuple[list[str], dict[str, object]]] = []
    own = _comment_line(99, "qa-user", QA_MARKER)
    monkeypatch.setattr(
        skill_commands.subprocess,
        "run",
        _fake_gh(calls, login_returncode=1, comment_lines=own),
    )

    skill_commands.post_qa_report()

    assert _lookup_command(calls) is None
    assert not _patch_commands(calls)
    assert calls[-1][0][:4] == ["gh", "pr", "comment", "42"]


def test_post_report_preserves_local_file_when_no_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_file = _stage_report(tmp_path, monkeypatch, "local QA evidence")
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(skill_commands.subprocess, "run", _fake_gh(calls, pr_number=""))

    skill_commands.post_qa_report()

    assert report_file.read_text(encoding="utf-8") == "local QA evidence"
    status_command = next(command for command, _ in calls if command[:3] == ["gh", "pr", "status"])
    assert status_command[status_command.index("--json") + 1] == "number,state"
    assert status_command[status_command.index("--jq") + 1] == (
        'if .currentBranch.state == "OPEN" then .currentBranch.number else empty end'
    )
    assert not any(command[:2] == ["gh", "api"] or command[:3] == ["gh", "pr", "comment"] for command, _ in calls)


@pytest.mark.skipif(
    not CLAUDE_QA_EVIDENCE_SKILL.exists(),
    reason=".claude/skills/qa-evidence/SKILL.md not present (OSS checkout)",
)
def test_qa_evidence_skill_mentions_linear_signed_upload_flow() -> None:
    skill_text = CLAUDE_QA_EVIDENCE_SKILL.read_text(encoding="utf-8")
    required_markers = [
        "GitHub has no public API",
        "GraphQL `fileUpload`",
        "public-file-urls-expire-in",
        "fileUpload(filename: $filename, contentType: $contentType, size: $size)",
        "commentCreate(input: { issueId: $issueId, body: $body })",
        "Do **not** embed the unsigned `assetUrl`",
        "31536000",
        "`<=3600` seconds",
        "warm GitHub's camo proxy",
        "github-pr-screenshot-evidence",
    ]
    for marker in required_markers:
        assert marker in skill_text


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
