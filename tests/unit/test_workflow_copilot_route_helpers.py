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
    _should_restore_persisted_workflow,
)
from skyvern.schemas.runs import ProxyLocation


def _agent_result(
    *,
    persisted: bool,
    unvalidated: bool = False,
    cancelled: bool = False,
    updated_workflow: Any = None,
    **kwargs: Any,
) -> MagicMock:
    """MagicMock with override flags explicitly set so a forgotten attr can't pass via MagicMock truthiness."""
    r = MagicMock()
    r.workflow_was_persisted = persisted
    r.unvalidated = unvalidated
    r.cancelled = cancelled
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


class TestEffectiveAutoAccept:
    def test_unvalidated_overrides_auto_accept(self) -> None:
        unvalidated = MagicMock()
        unvalidated.unvalidated = True
        unvalidated.cancelled = False

        assert _effective_auto_accept(True, unvalidated) is False
        assert _effective_auto_accept(False, unvalidated) is False

    def test_cancelled_overrides_auto_accept(self) -> None:
        cancelled = MagicMock()
        cancelled.unvalidated = False
        cancelled.cancelled = True

        assert _effective_auto_accept(True, cancelled) is False
        assert _effective_auto_accept(False, cancelled) is False

    def test_validated_proposal_respects_auto_accept_setting(self) -> None:
        validated = MagicMock()
        validated.unvalidated = False
        validated.cancelled = False

        assert _effective_auto_accept(True, validated) is True
        assert _effective_auto_accept(False, validated) is False
        assert _effective_auto_accept(None, validated) is False

    def test_no_agent_result_falls_back_to_user_setting(self) -> None:
        assert _effective_auto_accept(True, None) is True
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
