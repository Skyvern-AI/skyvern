"""Tests for the small pure helpers on workflow_copilot.py.

Covers the rollback/auto-accept safety net (``_should_restore_persisted_workflow``,
``_effective_auto_accept``, ``_proposal_disposition``) for the
``ENABLE_WORKFLOW_COPILOT_V2`` path, YAML normalization
(``_normalize_copilot_yaml``), prior-YAML resolution
(``_blockless_submission_fallback``, ``_prior_copilot_workflow_yaml``), and the
SSE terminal-frame invariant (``_ensure_terminal_frame``, SKY-9232).
"""

from __future__ import annotations

import asyncio
import textwrap
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.routes.workflow_copilot import (
    _blockless_submission_fallback,
    _effective_auto_accept,
    _ensure_terminal_frame,
    _normalize_copilot_yaml,
    _prior_copilot_workflow_yaml,
    _proposal_disposition,
    _should_restore_persisted_workflow,
)
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotStreamResponseUpdate
from skyvern.schemas.runs import ProxyLocation


def _agent_result(
    *,
    persisted: bool,
    proposal_disposition: str = "auto_applicable",
    cancelled: bool = False,
    updated_workflow: Any = None,
    canonical_was_persisted_due_to_param_change: bool = False,
    **kwargs: Any,
) -> MagicMock:
    """MagicMock with override flags explicitly set so a forgotten attr can't pass via MagicMock truthiness."""
    r = MagicMock()
    r.workflow_was_persisted = persisted
    r.proposal_disposition = proposal_disposition
    r.cancelled = cancelled
    r.updated_workflow = updated_workflow
    r.apply_without_review = kwargs.pop("apply_without_review", False)
    # SKY-10318: explicitly set the new staging flag so MagicMock truthiness
    # doesn't accidentally trigger the degraded-path branch in
    # `_should_restore_persisted_workflow`.
    r.canonical_was_persisted_due_to_param_change = canonical_was_persisted_due_to_param_change
    for k, v in kwargs.items():
        setattr(r, k, v)
    return r


class TestShouldRestorePersistedWorkflow:
    def test_restores_for_non_auto_accept_and_persisted_workflow(self) -> None:
        agent_result = _agent_result(persisted=True)

        assert _should_restore_persisted_workflow(False, agent_result) is True
        assert _should_restore_persisted_workflow(None, agent_result) is True

    def test_does_not_restore_for_auto_accept_or_unpersisted_result(self) -> None:
        persisted = _agent_result(persisted=True, updated_workflow=MagicMock())
        not_persisted = _agent_result(persisted=False)

        assert _should_restore_persisted_workflow(True, persisted) is False
        assert _should_restore_persisted_workflow(False, not_persisted) is False
        assert _should_restore_persisted_workflow(False, None) is False

    @pytest.mark.parametrize(
        "override_kwargs",
        [
            pytest.param({"proposal_disposition": "review_untested"}, id="review_untested"),
            pytest.param({"cancelled": True}, id="cancelled"),
            pytest.param({"proposal_disposition": "review_tested"}, id="review_tested"),
        ],
    )
    def test_wip_forces_rollback_under_auto_accept(self, override_kwargs: dict[str, Any]) -> None:
        agent_result = _agent_result(persisted=True, updated_workflow=MagicMock(), **override_kwargs)

        assert _should_restore_persisted_workflow(True, agent_result) is True
        assert _should_restore_persisted_workflow(False, agent_result) is True


class TestEffectiveAutoAccept:
    @pytest.mark.parametrize(
        ("proposal_disposition", "cancelled"),
        [
            pytest.param("review_untested", False, id="review_untested"),
            pytest.param("auto_applicable", True, id="cancelled"),
            pytest.param("review_tested", False, id="review_tested"),
            pytest.param("no_proposal", False, id="no_proposal"),
        ],
    )
    def test_disposition_or_cancellation_overrides_auto_accept(
        self, proposal_disposition: str, cancelled: bool
    ) -> None:
        result = MagicMock()
        result.proposal_disposition = proposal_disposition
        result.cancelled = cancelled

        assert _effective_auto_accept(True, result) is False
        assert _effective_auto_accept(False, result) is False

    def test_missing_proposal_disposition_is_no_proposal_without_updated_workflow(self) -> None:
        result = MagicMock(spec=["updated_workflow"])
        result.updated_workflow = None

        assert _proposal_disposition(result) == "no_proposal"

    def test_validated_proposal_respects_auto_accept_setting(self) -> None:
        validated = MagicMock()
        validated.proposal_disposition = "auto_applicable"
        validated.cancelled = False
        validated.apply_without_review = False

        assert _effective_auto_accept(True, validated) is True
        assert _effective_auto_accept(False, validated) is False
        assert _effective_auto_accept(None, validated) is False

    def test_apply_without_review_overrides_auto_accept_setting(self) -> None:
        validated = MagicMock()
        validated.proposal_disposition = "auto_applicable"
        validated.cancelled = False
        validated.apply_without_review = True

        assert _effective_auto_accept(False, validated) is True
        assert _effective_auto_accept(None, validated) is True

    def test_no_agent_result_is_not_auto_applicable(self) -> None:
        assert _proposal_disposition(None) == "no_proposal"
        assert _effective_auto_accept(True, None) is False
        assert _effective_auto_accept(False, None) is False


def test_response_update_schema_omits_legacy_review_flags() -> None:
    assert "unvalidated" not in WorkflowCopilotStreamResponseUpdate.model_fields
    assert "force_review" not in WorkflowCopilotStreamResponseUpdate.model_fields


class TestNormalizeCopilotYamlTitleCoercion:
    def test_missing_top_level_title_is_coerced_to_empty(self) -> None:
        yaml_str = "workflow_definition:\n  blocks: []\n  parameters: []\n"
        request = _normalize_copilot_yaml(yaml_str)
        assert request.title == ""

    def test_explicit_top_level_title_is_preserved(self) -> None:
        yaml_str = "title: My Workflow\nworkflow_definition:\n  blocks: []\n  parameters: []\n"
        request = _normalize_copilot_yaml(yaml_str)
        assert request.title == "My Workflow"


class TestNormalizeCopilotYamlBlockTypeAliases:
    def test_browser_task_alias_is_canonicalized_to_navigation(self) -> None:
        yaml_str = (
            "title: Browser Task Alias\n"
            "workflow_definition:\n"
            "  parameters: []\n"
            "  blocks:\n"
            "    - block_type: browser_task\n"
            "      label: open_picker\n"
            "      navigation_goal: Click the picker.\n"
        )

        request = _normalize_copilot_yaml(yaml_str)

        assert request.workflow_definition.blocks[0].block_type == "navigation"

    def test_nested_browser_task_alias_is_canonicalized_to_navigation(self) -> None:
        yaml_str = (
            "title: Nested Browser Task Alias\n"
            "workflow_definition:\n"
            "  parameters:\n"
            "    - parameter_type: workflow\n"
            "      key: items\n"
            "      workflow_parameter_type: json\n"
            "      default_value: '[]'\n"
            "  blocks:\n"
            "    - block_type: for_loop\n"
            "      label: loop_items\n"
            "      loop_over_parameter_key: items\n"
            "      loop_blocks:\n"
            "        - block_type: browser_task\n"
            "          label: click_item\n"
            "          navigation_goal: Click the current item.\n"
        )

        request = _normalize_copilot_yaml(yaml_str)

        loop_block = request.workflow_definition.blocks[0]
        assert loop_block.loop_blocks[0].block_type == "navigation"


class TestNormalizeCopilotYamlProxyLocation:
    def test_missing_proxy_location_is_preserved(self) -> None:
        yaml_str = "title: Proxy Workflow\nworkflow_definition:\n  blocks: []\n  parameters: []\n"

        request = _normalize_copilot_yaml(yaml_str)

        assert request.proxy_location is None

    def test_explicit_null_proxy_location_is_preserved(self) -> None:
        yaml_str = "title: Proxy Workflow\nproxy_location: null\nworkflow_definition:\n  blocks: []\n  parameters: []\n"

        request = _normalize_copilot_yaml(yaml_str)

        assert request.proxy_location is None

    @pytest.mark.parametrize(
        ("raw_value", "expected"),
        [
            ("US", ProxyLocation.RESIDENTIAL),
            ("USA", ProxyLocation.RESIDENTIAL),
            ("RESIDENTIAL_US", ProxyLocation.RESIDENTIAL),
            ("UK", ProxyLocation.RESIDENTIAL_GB),
            ("GB", ProxyLocation.RESIDENTIAL_GB),
            ("CA", ProxyLocation.RESIDENTIAL_CA),
            ("US_CA", ProxyLocation.US_CA),
            ("us-ny", ProxyLocation.US_NY),
        ],
    )
    def test_known_proxy_location_shorthands_are_canonicalized(self, raw_value: str, expected: ProxyLocation) -> None:
        yaml_str = (
            f"title: Proxy Workflow\n"
            f"proxy_location: {raw_value}\n"
            f"workflow_definition:\n"
            f"  blocks: []\n"
            f"  parameters: []\n"
        )

        request = _normalize_copilot_yaml(yaml_str)

        assert request.proxy_location == expected

    def test_unknown_proxy_location_still_fails_validation(self) -> None:
        yaml_str = "title: Proxy Workflow\nproxy_location: MARS\nworkflow_definition:\n  blocks: []\n  parameters: []\n"

        with pytest.raises(ValidationError):
            _normalize_copilot_yaml(yaml_str)


_PROPOSED_YAML = textwrap.dedent(
    """\
    title: t
    workflow_definition:
      parameters: []
      blocks:
        - block_type: goto_url
          label: open_site
          url: https://example.com
    """
)

_PERSISTED_YAML = textwrap.dedent(
    """\
    title: t
    workflow_definition:
      parameters: []
      blocks:
        - block_type: goto_url
          label: open_site
          url: https://example.com
        - block_type: navigation
          label: do_thing
          navigation_goal: Click the primary action.
    """
)

_USER_MODIFIED_YAML = _PROPOSED_YAML + (
    "    - block_type: text_prompt\n      label: summarize_result\n      llm_key: x\n      prompt: ok\n"
)


_BLOCKLESS_EXPLICIT_YAML = "title: t\nworkflow_definition:\n  parameters: []\n  blocks: []\n"


class TestBlocklessSubmissionFallback:
    def test_none_submission_with_prior_proposal_returns_fallback(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml=None,
            )
            == _PROPOSED_YAML
        )

    def test_empty_string_submission_with_prior_proposal_returns_fallback(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml="",
            )
            == _PROPOSED_YAML
        )

    def test_whitespace_only_submission_returns_fallback(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml="   \n",
            )
            == _PROPOSED_YAML
        )

    def test_explicit_blocks_empty_submission_is_NOT_overwritten(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml=_BLOCKLESS_EXPLICIT_YAML,
            )
            is None
        )

    def test_populated_submission_preserves_user_edit(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                submitted_workflow_yaml=_USER_MODIFIED_YAML,
            )
            is None
        )

    def test_no_proposal_returns_none(self) -> None:
        assert _blockless_submission_fallback(proposed_workflow=None, submitted_workflow_yaml="") is None

    def test_empty_dict_proposal_returns_none(self) -> None:
        assert _blockless_submission_fallback(proposed_workflow={}, submitted_workflow_yaml="") is None

    def test_non_string_copilot_yaml_returns_none(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": None},
                submitted_workflow_yaml="",
            )
            is None
        )

    def test_malformed_blockless_copilot_yaml_returns_none(self) -> None:
        assert (
            _blockless_submission_fallback(
                proposed_workflow={"_copilot_yaml": _BLOCKLESS_EXPLICIT_YAML},
                submitted_workflow_yaml="",
            )
            is None
        )


class TestPriorCopilotWorkflowYaml:
    def test_uses_proposal_when_present(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow={"_copilot_yaml": _PROPOSED_YAML},
                persisted_workflow_yaml=_PERSISTED_YAML,
            )
            == _PROPOSED_YAML
        )

    def test_falls_back_to_persisted_when_no_proposal(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow=None,
                persisted_workflow_yaml=_PERSISTED_YAML,
            )
            == _PERSISTED_YAML
        )

    def test_falls_back_to_persisted_when_proposal_has_no_copilot_yaml(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow={"some_other_field": "x"},
                persisted_workflow_yaml=_PERSISTED_YAML,
            )
            == _PERSISTED_YAML
        )

    def test_falls_back_to_persisted_when_copilot_yaml_is_blockless(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow={"_copilot_yaml": _BLOCKLESS_EXPLICIT_YAML},
                persisted_workflow_yaml=_PERSISTED_YAML,
            )
            == _PERSISTED_YAML
        )

    def test_returns_none_when_neither_has_blocks(self) -> None:
        assert (
            _prior_copilot_workflow_yaml(
                proposed_workflow={"_copilot_yaml": _BLOCKLESS_EXPLICIT_YAML},
                persisted_workflow_yaml=_BLOCKLESS_EXPLICIT_YAML,
            )
            is None
        )

    def test_returns_none_when_no_inputs(self) -> None:
        assert _prior_copilot_workflow_yaml(proposed_workflow=None, persisted_workflow_yaml=None) is None


class _FakeStream:
    def __init__(self, raise_on_send: BaseException | None = None) -> None:
        self.sent: list[Any] = []
        self._raise_on_send = raise_on_send

    async def send(self, message: Any) -> None:
        if self._raise_on_send is not None:
            raise self._raise_on_send
        self.sent.append(message)


@pytest.mark.asyncio
async def test_ensure_terminal_frame_noop_when_already_emitted() -> None:
    stream = _FakeStream()
    await _ensure_terminal_frame(stream, already_emitted=True)  # type: ignore[arg-type]
    assert stream.sent == []


@pytest.mark.asyncio
async def test_ensure_terminal_frame_sends_fallback_error_when_missing() -> None:
    stream = _FakeStream()
    await _ensure_terminal_frame(stream, already_emitted=False)  # type: ignore[arg-type]
    assert len(stream.sent) == 1
    frame = stream.sent[0]
    assert getattr(frame, "error", "").startswith("The assistant didn't finish")


@pytest.mark.asyncio
async def test_ensure_terminal_frame_swallows_send_exception() -> None:
    stream = _FakeStream(raise_on_send=RuntimeError("client already gone"))
    await _ensure_terminal_frame(stream, already_emitted=False)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ensure_terminal_frame_swallows_send_cancellation() -> None:
    stream = _FakeStream(raise_on_send=asyncio.CancelledError())
    await _ensure_terminal_frame(stream, already_emitted=False)  # type: ignore[arg-type]
