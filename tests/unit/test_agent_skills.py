from __future__ import annotations

from pathlib import Path

import pytest

from skyvern.forge.sdk.agents.skills import (
    MAX_PACKAGE_BYTES,
    MAX_SKILL_RESULT_CHARS,
    AvailableSkill,
    SkillPackage,
    SkillSession,
    skill_result_chars,
)
from tests.unit.copilot_test_helpers import available_skill, skill_md

LOGIN_BODY = "Tick 'Remember this device' at https://example.com/login before submitting.\n"


def session(*skills: AvailableSkill) -> SkillSession:
    return SkillSession(skills, agent="copilot")


def test_load_returns_body_and_files_then_already_loaded_without_body() -> None:
    skill = available_skill("example-portal-login", "organization", LOGIN_BODY, **{"reference/form.md": "Form notes"})
    skills = session(skill)

    assert skills.load("example-portal-login") == {
        "name": "example-portal-login",
        "version": skill.version,
        "body": LOGIN_BODY,
        "files": ["reference/form.md"],
    }
    assert skills.load("example-portal-login") == {
        "status": "already_loaded",
        "name": "example-portal-login",
        "version": skill.version,
    }
    assert session(skill).load("example-portal-login") == {
        "name": "example-portal-login",
        "version": skill.version,
        "body": LOGIN_BODY,
        "files": ["reference/form.md"],
    }


def test_read_resource_needs_no_load_and_unknown_file_lists_files() -> None:
    skills = session(
        available_skill("example-portal-login", "organization", LOGIN_BODY, **{"reference/form.md": "Form notes"})
    )

    assert skills.read_resource("example-portal-login", "reference/form.md") == {
        "name": "example-portal-login",
        "path": "reference/form.md",
        "text": "Form notes",
    }
    assert skills.read_resource("example-portal-login", "missing.md") == {
        "error": "unknown_file",
        "message": "example-portal-login has no file 'missing.md'.",
        "files": ["SKILL.md", "reference/form.md"],
    }


def test_unknown_skill_names_organization_and_workflow_skills_only() -> None:
    skills = session(
        available_skill("shared-helper", "everyone", LOGIN_BODY),
        available_skill("example-portal-login", "organization", LOGIN_BODY),
        available_skill("invoice-download", "workflow", LOGIN_BODY),
    )
    refusal = {
        "error": "unknown_skill",
        "message": "No skill is named 'nope'.",
        "names": ["example-portal-login", "invoice-download"],
    }

    assert skills.load("nope") == refusal
    assert skills.read_resource("nope", "SKILL.md") == refusal


def test_name_clash_resolves_workflow_over_organization_over_everyone() -> None:
    workflow = available_skill("example-portal-login", "workflow", "workflow\n")
    skills = session(
        available_skill("example-portal-login", "everyone", "everyone\n"),
        workflow,
        available_skill("example-portal-login", "organization", "organization\n"),
        available_skill("shared-helper", "everyone", LOGIN_BODY),
        available_skill("shared-helper", "organization", "organization\n"),
    )

    assert skills.owner("example-portal-login") == "workflow"
    assert skills.owner("shared-helper") == "organization"
    assert skills.owner("nope") is None
    assert skills.catalog_lines() == [
        "example-portal-login [workflow]: Log in to the example.com portal.",
        "shared-helper [organization]: Log in to the example.com portal.",
    ]
    assert skills.load("example-portal-login") == {
        "name": "example-portal-login",
        "version": workflow.version,
        "body": "workflow\n",
        "files": [],
    }


def test_skill_result_chars_measures_the_ascii_escaped_envelope() -> None:
    assert skill_result_chars({"name": "x", "path": "p", "text": "é"}) == len(
        '{"status": "completed", "output": {"name": "x", "path": "p", "text": "\\u00e9"}, "error": null}'
    )


def _largest_accepted(body_for: int) -> int:
    # Non-ASCII text costs six result characters per character, so len() under the limit can still be rejected.
    longest = 0
    low, high = 0, MAX_SKILL_RESULT_CHARS
    while low <= high:
        middle = (low + high) // 2
        try:
            SkillPackage.from_files({"SKILL.md": skill_md("example-portal-login", "é" * body_for + "a" * middle)})
            longest, low = middle, middle + 1
        except ValueError:
            high = middle - 1
    return longest


def test_size_rule_rejects_one_character_past_the_limit_with_non_ascii_text() -> None:
    padding = _largest_accepted(2_000)
    body = "é" * 2_000 + "a" * padding
    package = SkillPackage.from_files({"SKILL.md": skill_md("example-portal-login", body)})
    worst = max(
        skill_result_chars(
            {"name": package.name, "version": package.version, "body": package.body, "files": package.resource_paths}
        ),
        skill_result_chars({"name": package.name, "path": "SKILL.md", "text": skill_md(package.name, body)}),
    )

    assert worst == MAX_SKILL_RESULT_CHARS
    assert len(body) < MAX_SKILL_RESULT_CHARS - 9_000
    with pytest.raises(ValueError, match="result characters"):
        SkillPackage.from_files({"SKILL.md": skill_md("example-portal-login", body + "a")})
    with pytest.raises(ValueError, match="result characters"):
        SkillPackage.from_files({"SKILL.md": skill_md("example-portal-login"), "notes.md": "é" * 3_200})


@pytest.mark.parametrize("description", ['"two\\nlines"', "|\n  two\n  lines", "x" * 1025])
def test_description_must_be_one_line_of_at_most_1024_characters(description: str) -> None:
    text = f"---\nname: example-portal-login\ndescription: {description}\n---\nBody\n"
    with pytest.raises(ValueError, match="description"):
        SkillPackage.from_files({"SKILL.md": text})


@pytest.mark.parametrize("name", ["Example", "-example", "example-", "ex--ample", "x" * 65])
def test_name_must_follow_the_naming_constraints(name: str) -> None:
    with pytest.raises(ValueError, match="name"):
        SkillPackage.from_files({"SKILL.md": skill_md(name)})


def test_from_directory_requires_matching_folder_name_and_no_symlinks(tmp_path: Path) -> None:
    folder = tmp_path / "example-portal-login"
    folder.mkdir()
    (folder / "SKILL.md").write_text(skill_md("example-portal-login"))
    (folder / "scripts").mkdir()
    (folder / "scripts" / "fill.py").write_text("print('readable text, never run')\n")

    package = SkillPackage.from_directory(folder)
    assert package.resource_paths == ["scripts/fill.py"]

    renamed = tmp_path / "other-name"
    folder.rename(renamed)
    with pytest.raises(ValueError, match="frontmatter name"):
        SkillPackage.from_directory(renamed)

    (renamed / "link.md").symlink_to(renamed / "SKILL.md")
    with pytest.raises(ValueError, match="symbolic links"):
        SkillPackage.from_directory(renamed)


def test_from_directory_rejects_an_oversized_package_before_reading_any_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "example-portal-login"
    folder.mkdir()
    (folder / "SKILL.md").write_text(skill_md("example-portal-login"))
    (folder / "big.bin").write_bytes(b"\0" * (MAX_PACKAGE_BYTES + 1))

    def no_reads(*_: object, **__: object) -> str:
        raise AssertionError("a file was read")

    monkeypatch.setattr(Path, "read_text", no_reads)
    with pytest.raises(ValueError, match="bytes"):
        SkillPackage.from_directory(folder)
