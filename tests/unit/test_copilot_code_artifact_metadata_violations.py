"""Tests for the code-artifact-metadata validator returning every violation at once.

OSS-synced: only example.* / RFC-2606 placeholder targets and synthetic labels.
"""

from __future__ import annotations

import re
import textwrap

from skyvern.forge.sdk.copilot.tools import _normalize_code_artifact_metadata


def _code_block_yaml(label: str) -> str:
    return textwrap.dedent(
        f"""
        workflow_definition:
          blocks:
            - block_type: code
              label: {label}
              code: |
                await page.goto("https://example.com/")
        """
    ).strip()


def _violation_count(error: str) -> int:
    return len(re.findall(r"^\d+\.", error, flags=re.M))


def _broken_metadata(label: str) -> dict:
    return {
        "block_label": label,
        "artifact_id": "not-prefixed",
        "declared_goal": "do the thing",
        "claimed_outcomes": [{"id": "claim:x", "scope": "outcome", "text": "x", "status": "satisfied"}],
        "page_dependencies": [{"id": "dependency:p", "scope": "page", "status": "satisfied"}],
        "completion_criteria": [{"id": "criterion:c", "text": "c", "level": "terminal"}],
        "terminal_verifier_expectations": [{"id": "exp", "text": "e"}],
        "observation_refs": [{"observation_ref": "obs1", "status": "satisfied", "checkpoint_next_mode": "advance"}],
    }


class TestAccumulateAllViolations:
    def test_every_violation_returned_at_once(self) -> None:
        normalized, error = _normalize_code_artifact_metadata(
            [_broken_metadata("my_block")], _code_block_yaml("my_block")
        )
        assert normalized == {}
        assert error is not None
        # The 5+ sequential failures from the repair loop now surface together.
        assert _violation_count(error) >= 5
        assert error.startswith("Artifact metadata has ")
        assert "fix all of them in one update" in error
        assert "`artifact_id` to start with `code_artifact:`" in error
        assert "requires `source_tool`" in error
        assert "requires `depends_on`" in error
        assert "is `satisfied` but has no" in error

    def test_single_violation_is_not_numbered(self) -> None:
        metadata = {
            "block_label": "my_block",
            "artifact_id": "not-prefixed",
            "declared_goal": "g",
            "claimed_outcomes": [
                {
                    "id": "claim:x",
                    "scope": "outcome",
                    "text": "x",
                    "status": "observed_not_verified",
                    "depends_on": ["dependency:p"],
                    "covered_criteria": ["criterion:c"],
                    "observation_refs": ["obs1"],
                }
            ],
            "page_dependencies": [
                {"id": "dependency:p", "scope": "page", "status": "observed_not_verified", "observation_refs": ["obs1"]}
            ],
            "completion_criteria": [{"id": "criterion:c", "text": "c", "level": "terminal"}],
            "terminal_verifier_expectations": [{"id": "exp", "text": "e", "criteria_ids": ["criterion:c"]}],
            "observation_refs": [
                {
                    "observation_ref": "obs1",
                    "dependency_id": "dependency:p",
                    "status": "observed_not_verified",
                    "source_tool": "scout_interaction",
                }
            ],
        }
        normalized, error = _normalize_code_artifact_metadata([metadata], _code_block_yaml("my_block"))
        assert normalized == {}
        assert error is not None
        assert _violation_count(error) == 0
        assert error == "Artifact metadata for `my_block` requires `artifact_id` to start with `code_artifact:`."

    def test_violations_aggregate_across_multiple_artifacts(self) -> None:
        yaml = textwrap.dedent(
            """
            workflow_definition:
              blocks:
                - block_type: code
                  label: block_one
                  code: |
                    await page.goto("https://example.com/")
                - block_type: code
                  label: block_two
                  code: |
                    await page.goto("https://example.com/")
            """
        ).strip()
        normalized, error = _normalize_code_artifact_metadata(
            [_broken_metadata("block_one"), _broken_metadata("block_two")], yaml
        )
        assert normalized == {}
        assert error is not None
        assert "block_one" in error
        assert "block_two" in error

    def test_unknown_label_does_not_short_circuit_other_artifacts(self) -> None:
        normalized, error = _normalize_code_artifact_metadata(
            [_broken_metadata("ghost_label"), _broken_metadata("my_block")], _code_block_yaml("my_block")
        )
        assert normalized == {}
        assert error is not None
        assert "does not reference an existing code block label" in error
        # The second artifact's shape violations are still surfaced.
        assert "requires `source_tool`" in error

    def test_valid_metadata_passes(self) -> None:
        metadata = {
            "block_label": "my_block",
            "artifact_id": "code_artifact:my_block",
            "declared_goal": "g",
            "claimed_outcomes": [
                {
                    "id": "claim:x",
                    "scope": "outcome",
                    "text": "x",
                    "status": "observed_not_verified",
                    "depends_on": ["dependency:p"],
                    "covered_criteria": ["criterion:c"],
                    "observation_refs": ["obs1"],
                }
            ],
            "page_dependencies": [
                {"id": "dependency:p", "scope": "page", "status": "observed_not_verified", "observation_refs": ["obs1"]}
            ],
            "completion_criteria": [{"id": "criterion:c", "text": "c", "level": "terminal"}],
            "terminal_verifier_expectations": [{"id": "exp", "text": "e", "criteria_ids": ["criterion:c"]}],
            "observation_refs": [
                {
                    "observation_ref": "obs1",
                    "dependency_id": "dependency:p",
                    "status": "observed_not_verified",
                    "source_tool": "scout_interaction",
                }
            ],
        }
        normalized, error = _normalize_code_artifact_metadata([metadata], _code_block_yaml("my_block"))
        assert error is None
        assert list(normalized.keys()) == ["my_block"]

    def test_empty_metadata_is_noop(self) -> None:
        assert _normalize_code_artifact_metadata(None, _code_block_yaml("my_block")) == ({}, None)
        assert _normalize_code_artifact_metadata([], _code_block_yaml("my_block")) == ({}, None)
