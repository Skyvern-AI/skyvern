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


def _valid_metadata(label: str) -> dict:
    return {
        "block_label": label,
        "artifact_id": f"code_artifact:{label}",
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
        assert "requires `source_tool`" in error
        assert "requires `depends_on`" in error
        assert "is `satisfied` but has no" in error

    def test_non_conforming_artifact_id_is_imposed_not_rejected(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata["artifact_id"] = "not-prefixed"
        normalized, error = _normalize_code_artifact_metadata([metadata], _code_block_yaml("my_block"))
        assert error is None
        assert normalized["my_block"]["artifact_id"] == "code_artifact:my_block"

    def test_single_violation_is_not_numbered(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata["claimed_outcomes"][0].pop("depends_on")
        normalized, error = _normalize_code_artifact_metadata([metadata], _code_block_yaml("my_block"))
        assert normalized == {}
        assert error is not None
        assert _violation_count(error) == 0
        assert error == "Artifact metadata claim `claim:x` for `my_block` requires `depends_on`."

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

    def test_unknown_label_is_dropped_and_other_artifacts_still_validated(self) -> None:
        normalized, error = _normalize_code_artifact_metadata(
            [_broken_metadata("ghost_label"), _broken_metadata("my_block")], _code_block_yaml("my_block")
        )
        assert normalized == {}
        assert error is not None
        # The stale entry is pruned, never rejected; the anchored artifact's
        # shape violations are still surfaced.
        assert "ghost_label" not in error
        assert "requires `source_tool`" in error

    def test_valid_metadata_passes(self) -> None:
        normalized, error = _normalize_code_artifact_metadata(
            [_valid_metadata("my_block")], _code_block_yaml("my_block")
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]

    def test_empty_metadata_is_noop(self) -> None:
        assert _normalize_code_artifact_metadata(None, _code_block_yaml("my_block")) == ({}, None)
        assert _normalize_code_artifact_metadata([], _code_block_yaml("my_block")) == ({}, None)


_SCOUT_TRAJECTORY = [
    {
        "tool_name": "click",
        "selector": "#search-submit",
        "source_url": "https://registry.example.com/search",
        "role": "button",
        "accessible_name": "Search",
        "trajectory_index": 0,
    },
]


def _assert_passes_full_validator(row: dict) -> None:
    """The imposed row must conform without any trajectory-driven defaulting."""
    renormalized, error = _normalize_code_artifact_metadata([row], _code_block_yaml(row["block_label"]))
    assert error is None, error
    assert list(renormalized.keys()) == [row["block_label"]]


class TestSeamImposition:
    def test_minimal_metadata_is_fully_defaulted(self) -> None:
        metadata = {"block_label": "my_block", "declared_goal": "Search the registry and expand result rows"}
        normalized, error = _normalize_code_artifact_metadata(
            [metadata], _code_block_yaml("my_block"), impose_defaults=True, scout_trajectory=_SCOUT_TRAJECTORY
        )
        assert error is None
        row = normalized["my_block"]
        assert row["artifact_id"] == "code_artifact:my_block"
        assert row["page_dependencies"]
        assert row["claimed_outcomes"]
        assert row["completion_criteria"]
        assert row["terminal_verifier_expectations"]
        assert row["observation_refs"]
        _assert_passes_full_validator(row)

    def test_omitted_page_dependencies_filled_from_trajectory(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata.pop("page_dependencies")
        normalized, error = _normalize_code_artifact_metadata(
            [metadata], _code_block_yaml("my_block"), impose_defaults=True, scout_trajectory=_SCOUT_TRAJECTORY
        )
        assert error is None
        dependency = normalized["my_block"]["page_dependencies"][0]
        assert dependency["status"] == "observed_not_verified"
        assert dependency["url_hint"] == "https://registry.example.com/search"
        _assert_passes_full_validator(normalized["my_block"])

    def test_omitted_claims_derived_from_declared_goal(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata.pop("claimed_outcomes")
        normalized, error = _normalize_code_artifact_metadata(
            [metadata], _code_block_yaml("my_block"), impose_defaults=True, scout_trajectory=_SCOUT_TRAJECTORY
        )
        assert error is None
        claim = normalized["my_block"]["claimed_outcomes"][0]
        assert claim["text"] == "g"
        assert claim["status"] == "observed_not_verified"
        assert claim["depends_on"] == ["dependency:p"]
        _assert_passes_full_validator(normalized["my_block"])

    def test_omitted_criteria_derived_from_claims(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata.pop("completion_criteria")
        metadata["claimed_outcomes"][0].pop("covered_criteria")
        normalized, error = _normalize_code_artifact_metadata(
            [metadata], _code_block_yaml("my_block"), impose_defaults=True, scout_trajectory=_SCOUT_TRAJECTORY
        )
        assert error is None
        row = normalized["my_block"]
        criterion = row["completion_criteria"][0]
        assert criterion["level"] == "terminal"
        assert criterion["text"] == "x"
        assert row["claimed_outcomes"][0]["covered_criteria"] == [criterion["id"]]
        _assert_passes_full_validator(row)

    def test_omitted_expectations_linked_to_criteria(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata.pop("terminal_verifier_expectations")
        normalized, error = _normalize_code_artifact_metadata(
            [metadata], _code_block_yaml("my_block"), impose_defaults=True, scout_trajectory=_SCOUT_TRAJECTORY
        )
        assert error is None
        expectation = normalized["my_block"]["terminal_verifier_expectations"][0]
        assert expectation["criteria_ids"] == ["criterion:c"]
        _assert_passes_full_validator(normalized["my_block"])

    def test_omitted_artifact_refs_default_to_scout_observation(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata.pop("observation_refs")
        normalized, error = _normalize_code_artifact_metadata(
            [metadata], _code_block_yaml("my_block"), impose_defaults=True, scout_trajectory=_SCOUT_TRAJECTORY
        )
        assert error is None
        ref = normalized["my_block"]["observation_refs"][0]
        assert ref["source_tool"] == "scout_interaction"
        assert ref["status"] == "observed_not_verified"
        _assert_passes_full_validator(normalized["my_block"])

    def test_missing_ref_source_tool_and_scope_filled(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata["observation_refs"] = [{"observation_ref": "obs1", "status": "observed_not_verified"}]
        normalized, error = _normalize_code_artifact_metadata(
            [metadata], _code_block_yaml("my_block"), impose_defaults=True, scout_trajectory=_SCOUT_TRAJECTORY
        )
        assert error is None
        ref = normalized["my_block"]["observation_refs"][0]
        assert ref["source_tool"] == "scout_interaction"
        assert ref["dependency_id"] == "dependency:p"
        _assert_passes_full_validator(normalized["my_block"])

    def test_contradictory_checkpoint_advance_dropped(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata["observation_refs"][0]["checkpoint_next_mode"] = "advance"
        normalized, error = _normalize_code_artifact_metadata(
            [metadata], _code_block_yaml("my_block"), impose_defaults=True, scout_trajectory=_SCOUT_TRAJECTORY
        )
        assert error is None
        assert "checkpoint_next_mode" not in normalized["my_block"]["observation_refs"][0]

    def test_satisfied_claim_without_evidence_is_undefaultable(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata["claimed_outcomes"][0]["status"] = "satisfied"
        normalized, error = _normalize_code_artifact_metadata(
            [metadata], _code_block_yaml("my_block"), impose_defaults=True, scout_trajectory=_SCOUT_TRAJECTORY
        )
        assert normalized == {}
        assert error is not None
        assert "is `satisfied` but has no" in error

    def test_no_imposition_when_not_imposing(self) -> None:
        metadata = {"block_label": "my_block", "declared_goal": "g"}
        normalized, error = _normalize_code_artifact_metadata([metadata], _code_block_yaml("my_block"))
        assert normalized == {}
        assert error is not None
        assert "requires non-empty" in error

    def test_imposition_works_without_scout_trajectory(self) -> None:
        # The run-3 class: model-authored per-entry refs missing the scoped id
        # and source_tool must be normalized, not rejected, even when no scout
        # interaction was recorded before authoring.
        metadata = {
            "block_label": "my_block",
            "declared_goal": "g",
            "observation_refs": [{"observation_ref": "obs1", "status": "observed_not_verified"}],
        }
        normalized, error = _normalize_code_artifact_metadata(
            [metadata], _code_block_yaml("my_block"), impose_defaults=True
        )
        assert error is None
        ref = normalized["my_block"]["observation_refs"][0]
        assert ref["dependency_id"]
        assert ref["source_tool"] == "scout_interaction"
        assert "url_hint" not in normalized["my_block"]["page_dependencies"][0]
        _assert_passes_full_validator(normalized["my_block"])

    def test_imposition_fills_missing_mechanical_ids(self) -> None:
        metadata = {
            "block_label": "my_block",
            "declared_goal": "g",
            "claimed_outcomes": [{"text": "rows visible"}],
            "completion_criteria": [{"text": "rows shown"}],
        }
        normalized, error = _normalize_code_artifact_metadata(
            [metadata], _code_block_yaml("my_block"), impose_defaults=True, scout_trajectory=_SCOUT_TRAJECTORY
        )
        assert error is None
        row = normalized["my_block"]
        assert row["claimed_outcomes"][0]["id"]
        assert row["claimed_outcomes"][0]["scope"] == "outcome"
        assert row["completion_criteria"][0]["id"]
        _assert_passes_full_validator(row)


class TestPerLabelSalvage:
    def test_conforming_label_survives_offending_label(self) -> None:
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
        bad = _valid_metadata("block_two")
        bad["claimed_outcomes"][0]["status"] = "satisfied"
        normalized, error = _normalize_code_artifact_metadata([_valid_metadata("block_one"), bad], yaml)
        assert list(normalized.keys()) == ["block_one"]
        assert error is not None
        assert "block_two" in error
        assert "block_one" not in error

    def test_unknown_label_dropped_alone(self) -> None:
        normalized, error = _normalize_code_artifact_metadata(
            [_valid_metadata("ghost_label"), _valid_metadata("my_block")], _code_block_yaml("my_block")
        )
        assert list(normalized.keys()) == ["my_block"]
        assert error is None


class TestStaleLabelRekey:
    def test_single_stale_entry_rekeys_to_single_uncovered_label(self) -> None:
        normalized, error = _normalize_code_artifact_metadata(
            [_valid_metadata("stale_label")], _code_block_yaml("my_block")
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]
        assert normalized["my_block"]["block_label"] == "my_block"
        assert normalized["my_block"]["artifact_id"] == "code_artifact:my_block"

    def test_label_less_entry_rekeys_to_single_uncovered_label(self) -> None:
        metadata = _valid_metadata("my_block")
        metadata.pop("block_label")
        normalized, error = _normalize_code_artifact_metadata([metadata], _code_block_yaml("my_block"))
        assert error is None
        assert list(normalized.keys()) == ["my_block"]

    def test_multiple_stale_entries_are_dropped_not_rekeyed(self) -> None:
        normalized, error = _normalize_code_artifact_metadata(
            [_valid_metadata("ghost_a"), _valid_metadata("ghost_b")], _code_block_yaml("my_block")
        )
        assert normalized == {}
        assert error is None

    def test_dropped_stale_entry_leaves_skeleton_for_uncovered_label_when_imposing(self) -> None:
        normalized, error = _normalize_code_artifact_metadata(
            [_valid_metadata("ghost_a"), _valid_metadata("ghost_b")],
            _code_block_yaml("my_block"),
            impose_defaults=True,
            scout_trajectory=_SCOUT_TRAJECTORY,
        )
        assert error is None
        assert list(normalized.keys()) == ["my_block"]
        row = normalized["my_block"]
        assert row["declared_goal"]
        assert row["page_dependencies"]
        assert row["observation_refs"]
        _assert_passes_full_validator(row)

    def test_partial_coverage_gets_skeleton_for_uncovered_label_when_imposing(self) -> None:
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
            [_valid_metadata("block_one")],
            yaml,
            impose_defaults=True,
            scout_trajectory=_SCOUT_TRAJECTORY,
        )
        assert error is None
        assert sorted(normalized.keys()) == ["block_one", "block_two"]
        _assert_passes_full_validator(normalized["block_two"])

    def test_duplicate_label_keeps_first_entry(self) -> None:
        first = _valid_metadata("my_block")
        second = _valid_metadata("my_block")
        second["declared_goal"] = "different"
        normalized, error = _normalize_code_artifact_metadata([first, second], _code_block_yaml("my_block"))
        assert error is None
        assert normalized["my_block"]["declared_goal"] == "g"
