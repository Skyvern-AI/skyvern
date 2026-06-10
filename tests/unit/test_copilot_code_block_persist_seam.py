"""Tests for the code-block persist seam in `_update_workflow`.

OSS-synced: only example.* / RFC-2606 placeholder targets and synthetic labels.
"""

from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot.blocker_signal import assert_clean_user_facing_text
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.tools import (
    _code_block_safety_errors,
    _detect_stale_block_metadata,
    _update_workflow,
)


def _yaml(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


_IMPORTING_CODE_YAML = _yaml(
    """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          import asyncio
          await page.goto("https://example.com/search")
    """
)

_SAFE_CODE_YAML = _yaml(
    """
    title: Registry lookup
    workflow_definition:
      blocks:
      - block_type: code
        label: search_registry
        code: |
          await page.goto("https://example.com/search")
    """
)


def _code_only_ctx() -> CopilotContext:
    ctx = CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.scout_trajectory = [
        {
            "tool_name": "click",
            "selector": "#search-submit",
            "source_url": "https://example.com/search",
            "trajectory_index": 0,
        }
    ]
    return ctx


class TestCodeSafetySeam:
    def test_import_in_new_code_block_is_a_seam_error(self) -> None:
        errors = _code_block_safety_errors(_IMPORTING_CODE_YAML, None)
        assert len(errors) == 1
        assert "search_registry" in errors[0]
        assert "Not allowed to import modules" in errors[0]

    def test_unchanged_legacy_code_block_is_not_rechecked(self) -> None:
        assert _code_block_safety_errors(_IMPORTING_CODE_YAML, _IMPORTING_CODE_YAML) == []

    def test_changed_code_block_is_rechecked(self) -> None:
        assert _code_block_safety_errors(_IMPORTING_CODE_YAML, _SAFE_CODE_YAML)

    def test_safe_code_passes(self) -> None:
        assert _code_block_safety_errors(_SAFE_CODE_YAML, None) == []

    def test_syntax_error_is_a_seam_error(self) -> None:
        broken = _SAFE_CODE_YAML.replace('await page.goto("https://example.com/search")', "await page.goto(")
        errors = _code_block_safety_errors(broken, None)
        assert len(errors) == 1
        assert "not valid Python" in errors[0]

    @pytest.mark.asyncio
    async def test_update_workflow_rejects_import_before_any_run(self) -> None:
        ctx = _code_only_ctx()
        result = await _update_workflow({"workflow_yaml": _IMPORTING_CODE_YAML}, ctx)
        assert result["ok"] is False
        assert "Not allowed to import modules" in result["error"]
        assert "import" not in result["user_facing_summary"]
        assert result["user_facing_summary"]

    @pytest.mark.asyncio
    async def test_code_rejection_does_not_salvage_metadata_into_ctx(self) -> None:
        ctx = _code_only_ctx()
        metadata = [{"block_label": "search_registry", "declared_goal": "search the registry"}]
        result = await _update_workflow(
            {"workflow_yaml": _IMPORTING_CODE_YAML, "code_artifact_metadata": metadata}, ctx
        )
        assert result["ok"] is False
        assert ctx.code_artifact_metadata == {}


class TestSeamSalvageIntoContext:
    @pytest.mark.asyncio
    async def test_conforming_label_persists_when_sibling_label_rejects(self) -> None:
        workflow_yaml = _yaml(
            """
            title: Registry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: block_one
                code: |
                  await page.goto("https://example.com/search")
              - block_type: code
                label: block_two
                code: |
                  await page.goto("https://example.com/results")
            """
        )
        metadata = [
            {"block_label": "block_one", "declared_goal": "search the registry"},
            {
                "block_label": "block_two",
                "declared_goal": "expand the result rows",
                "claimed_outcomes": [
                    {"id": "claim:rows", "scope": "outcome", "text": "rows expanded", "status": "satisfied"}
                ],
            },
        ]
        ctx = _code_only_ctx()
        result = await _update_workflow({"workflow_yaml": workflow_yaml, "code_artifact_metadata": metadata}, ctx)

        assert result["ok"] is False
        assert "block_two" in result["error"]
        assert "block_one" not in result["error"]
        assert list(ctx.code_artifact_metadata.keys()) == ["block_one"]
        assert ctx.workflow_verification_evidence.code_artifact_metadata == ctx.code_artifact_metadata
        assert "contract violation" not in result["user_facing_summary"]
        assert "`" not in result["user_facing_summary"]

    @pytest.mark.asyncio
    async def test_stale_entry_dropped_and_draft_metadata_survives_unaccepted_submission(self) -> None:
        draft_yaml = _yaml(
            """
            title: Registry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: block_a
                code: |
                  await page.goto("https://example.com/search")
              - block_type: code
                label: block_b
                code: |
                  await page.goto("https://example.com/results")
            """
        )
        submitted_yaml = _yaml(
            """
            title: Registry lookup
            workflow_definition:
              blocks:
              - block_type: code
                label: block_a
                code: |
                  await page.goto("https://example.com/search")
            """
        )
        ctx = _code_only_ctx()
        ctx.workflow_yaml = draft_yaml
        stored_block_b = {"block_label": "block_b", "declared_goal": "expand the result rows"}
        ctx.code_artifact_metadata = {
            "block_a": {"block_label": "block_a", "declared_goal": "search the registry"},
            "block_b": stored_block_b,
        }
        ctx.workflow_verification_evidence.code_artifact_metadata = dict(ctx.code_artifact_metadata)
        metadata = [
            {"block_label": "block_a", "declared_goal": "search the registry"},
            {"block_label": "ghost", "declared_goal": "does not exist"},
        ]

        result = await _update_workflow({"workflow_yaml": submitted_yaml, "code_artifact_metadata": metadata}, ctx)

        # The stale entry is pruned at the seam; the submission proceeds and
        # only non-metadata gates may reject it, so prior-draft metadata stays.
        error_text = str(result.get("error") or "")
        assert "Artifact metadata" not in error_text
        assert "ghost" not in error_text
        assert sorted(ctx.code_artifact_metadata) == ["block_a", "block_b"]
        assert ctx.code_artifact_metadata["block_b"] == stored_block_b
        assert ctx.workflow_verification_evidence.code_artifact_metadata == ctx.code_artifact_metadata

    @pytest.mark.asyncio
    async def test_minimal_metadata_with_trajectory_produces_no_violation_error(self) -> None:
        ctx = _code_only_ctx()
        metadata = [{"block_label": "search_registry", "declared_goal": "search the registry"}]
        result = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML, "code_artifact_metadata": metadata}, ctx)
        # The seam may reject later (credential checks need the app); the metadata
        # contract itself must not be the rejection.
        error_text = str(result.get("error") or "")
        assert "Artifact metadata" not in error_text
        assert "contract violation" not in error_text
        assert ctx.code_artifact_metadata["search_registry"]["artifact_id"] == "code_artifact:search_registry"


class TestStaleLabelSeamFlow:
    @pytest.mark.asyncio
    async def test_stale_metadata_label_rekeys_without_any_stale_rejection(self) -> None:
        # Run-2 shape: metadata keyed to a label absent from the submitted
        # YAML. The seam re-keys it to the only uncovered code block, so
        # neither the metadata gate nor the stale-block-metadata validation
        # path can bounce the submission back to the model.
        ctx = _code_only_ctx()
        ctx.workflow_yaml = _SAFE_CODE_YAML
        metadata = [{"block_label": "search_certificant_stale", "declared_goal": "search the registry"}]

        result = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML, "code_artifact_metadata": metadata}, ctx)

        error_text = str(result.get("error") or "")
        assert "Artifact metadata" not in error_text
        assert "still appears stale" not in error_text
        assert list(ctx.code_artifact_metadata.keys()) == ["search_registry"]
        assert ctx.code_artifact_metadata["search_registry"]["artifact_id"] == "code_artifact:search_registry"
        # The seam never rewrites YAML labels, so its output cannot trip the
        # stale-block-metadata validation that fires on label/title renames.
        assert _detect_stale_block_metadata(_SAFE_CODE_YAML, ctx.workflow_yaml) == []

    @pytest.mark.asyncio
    async def test_malformed_per_entry_refs_normalize_without_scout_interactions(self) -> None:
        # Run-3 shape: model-authored observation_refs rows missing the scoped
        # id, authored before any scout interaction was recorded.
        ctx = _code_only_ctx()
        ctx.scout_trajectory = []
        metadata = [
            {
                "block_label": "search_registry",
                "declared_goal": "search the registry",
                "observation_refs": [{"observation_ref": "obs1", "status": "observed_not_verified"}],
            }
        ]

        result = await _update_workflow({"workflow_yaml": _SAFE_CODE_YAML, "code_artifact_metadata": metadata}, ctx)

        error_text = str(result.get("error") or "")
        assert "Artifact metadata" not in error_text
        assert "contract violation" not in error_text
        ref = ctx.code_artifact_metadata["search_registry"]["observation_refs"][0]
        assert ref["dependency_id"]
        assert ref["source_tool"]


def test_run_id_leak_check_covers_non_numeric_ids() -> None:
    with pytest.raises(ValueError):
        assert_clean_user_facing_text("Outcome uncertain for wr_sample_123abc.")
