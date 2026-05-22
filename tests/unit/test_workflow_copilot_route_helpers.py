"""Tests for the additive helpers landed on workflow_copilot.py in PR 7.

``_should_restore_persisted_workflow`` and ``_restore_workflow_definition`` are
the rollback safety net for the ``ENABLE_WORKFLOW_COPILOT_V2`` path: without
them a client disconnect or mid-stream agent failure would leave the workflow
mutated on disk. These tests were deferred from PR 6's
``test_copilot_sdk_contracts.py`` because the helpers only exist after PR 7's
hand-edit lands.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.routes.workflow_copilot import (
    _effective_auto_accept,
    _normalize_copilot_yaml,
    _proposal_disposition,
    _should_restore_persisted_workflow,
)
from skyvern.schemas.runs import ProxyLocation


def _agent_result(
    *,
    persisted: bool,
    unvalidated: bool = False,
    cancelled: bool = False,
    force_review: bool = False,
    updated_workflow: Any = None,
    **kwargs: Any,
) -> MagicMock:
    """MagicMock with override flags explicitly set so a forgotten attr can't pass via MagicMock truthiness."""
    r = MagicMock()
    r.workflow_was_persisted = persisted
    r.proposal_disposition = (
        "review_untested" if unvalidated else "review_tested" if force_review else "auto_applicable"
    )
    r.unvalidated = unvalidated
    r.cancelled = cancelled
    r.force_review = force_review
    r.updated_workflow = updated_workflow
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

    def test_unvalidated_timeout_wip_forces_rollback_under_auto_accept(self) -> None:
        agent_result = _agent_result(persisted=True, unvalidated=True, updated_workflow=MagicMock())

        assert _should_restore_persisted_workflow(True, agent_result) is True
        assert _should_restore_persisted_workflow(False, agent_result) is True

    def test_cancelled_wip_forces_rollback_under_auto_accept(self) -> None:
        agent_result = _agent_result(persisted=True, cancelled=True, updated_workflow=MagicMock())

        assert _should_restore_persisted_workflow(True, agent_result) is True
        assert _should_restore_persisted_workflow(False, agent_result) is True

    def test_force_review_wip_forces_rollback_under_auto_accept(self) -> None:
        agent_result = _agent_result(persisted=True, force_review=True, updated_workflow=MagicMock())

        assert _should_restore_persisted_workflow(True, agent_result) is True
        assert _should_restore_persisted_workflow(False, agent_result) is True


class TestEffectiveAutoAccept:
    def test_unvalidated_overrides_auto_accept(self) -> None:
        unvalidated = MagicMock()
        unvalidated.unvalidated = True
        unvalidated.cancelled = False
        unvalidated.force_review = False

        assert _effective_auto_accept(True, unvalidated) is False
        assert _effective_auto_accept(False, unvalidated) is False

    def test_cancelled_overrides_auto_accept(self) -> None:
        cancelled = MagicMock()
        cancelled.unvalidated = False
        cancelled.cancelled = True
        cancelled.force_review = False

        assert _effective_auto_accept(True, cancelled) is False
        assert _effective_auto_accept(False, cancelled) is False

    def test_force_review_overrides_auto_accept(self) -> None:
        force_review = MagicMock()
        force_review.proposal_disposition = "review_tested"
        force_review.unvalidated = False
        force_review.cancelled = False
        force_review.force_review = True

        assert _effective_auto_accept(True, force_review) is False
        assert _effective_auto_accept(False, force_review) is False

    def test_review_untested_disposition_overrides_auto_accept(self) -> None:
        result = MagicMock()
        result.proposal_disposition = "review_untested"
        result.cancelled = False

        assert _effective_auto_accept(True, result) is False
        assert _effective_auto_accept(False, result) is False

    def test_no_proposal_disposition_overrides_auto_accept(self) -> None:
        result = MagicMock()
        result.proposal_disposition = "no_proposal"
        result.cancelled = False

        assert _effective_auto_accept(True, result) is False
        assert _effective_auto_accept(False, result) is False

    def test_missing_proposal_disposition_is_no_proposal_without_updated_workflow(self) -> None:
        result = MagicMock(spec=["unvalidated", "force_review", "updated_workflow"])
        result.unvalidated = False
        result.force_review = False
        result.updated_workflow = None

        assert _proposal_disposition(result) == "no_proposal"

    def test_legacy_flag_fallback_for_stacked_deploy(self) -> None:
        result = MagicMock(spec=["unvalidated", "force_review"])
        result.unvalidated = False
        result.force_review = True

        assert _proposal_disposition(result) == "review_tested"

    def test_validated_proposal_respects_auto_accept_setting(self) -> None:
        validated = MagicMock()
        validated.unvalidated = False
        validated.cancelled = False
        validated.force_review = False

        assert _effective_auto_accept(True, validated) is True
        assert _effective_auto_accept(False, validated) is False
        assert _effective_auto_accept(None, validated) is False

    def test_no_agent_result_is_not_auto_applicable(self) -> None:
        assert _proposal_disposition(None) == "no_proposal"
        assert _effective_auto_accept(True, None) is False
        assert _effective_auto_accept(False, None) is False


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
