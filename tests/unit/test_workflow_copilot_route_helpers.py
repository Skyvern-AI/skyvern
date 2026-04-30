"""Tests for the additive helpers landed on workflow_copilot.py in PR 7.

``_should_restore_persisted_workflow`` and ``_restore_workflow_definition`` are
the rollback safety net for the ``ENABLE_WORKFLOW_COPILOT_V2`` path: without
them a client disconnect or mid-stream agent failure would leave the workflow
mutated on disk. These tests were deferred from PR 6's
``test_copilot_sdk_contracts.py`` because the helpers only exist after PR 7's
hand-edit lands.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from skyvern.forge.sdk.routes.workflow_copilot import _normalize_copilot_yaml, _should_restore_persisted_workflow


class TestShouldRestorePersistedWorkflow:
    def test_restores_for_non_auto_accept_and_persisted_workflow(self) -> None:
        agent_result = MagicMock()
        agent_result.workflow_was_persisted = True

        assert _should_restore_persisted_workflow(False, agent_result) is True
        assert _should_restore_persisted_workflow(None, agent_result) is True

    def test_does_not_restore_for_auto_accept_or_unpersisted_result(self) -> None:
        persisted = MagicMock()
        persisted.workflow_was_persisted = True
        not_persisted = MagicMock()
        not_persisted.workflow_was_persisted = False

        assert _should_restore_persisted_workflow(True, persisted) is False
        assert _should_restore_persisted_workflow(False, not_persisted) is False
        assert _should_restore_persisted_workflow(False, None) is False


class TestNormalizeCopilotYamlTitleCoercion:
    def test_missing_top_level_title_is_coerced_to_empty(self) -> None:
        yaml_str = "workflow_definition:\n  blocks: []\n  parameters: []\n"
        request = _normalize_copilot_yaml(yaml_str)
        assert request.title == ""

    def test_explicit_top_level_title_is_preserved(self) -> None:
        yaml_str = "title: My Workflow\nworkflow_definition:\n  blocks: []\n  parameters: []\n"
        request = _normalize_copilot_yaml(yaml_str)
        assert request.title == "My Workflow"
