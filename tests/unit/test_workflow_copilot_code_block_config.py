"""Tests for Workflow Copilot code-block config selection."""

from __future__ import annotations

import pytest

from skyvern.config import settings
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.copilot.config import (
    BlockAuthoringPolicy,
    CopilotConfig,
    block_authoring_policy_from_code_only_mode,
    download_scout_act_required_for_policy,
)


def test_copilot_config_defaults_to_standard_policy() -> None:
    assert CopilotConfig().block_authoring_policy == BlockAuthoringPolicy.STANDARD


def test_code_block_settings_helper_selects_policy() -> None:
    assert block_authoring_policy_from_code_only_mode(True) == BlockAuthoringPolicy.CODE_ONLY_BROWSER
    assert block_authoring_policy_from_code_only_mode(False) == BlockAuthoringPolicy.STANDARD


def test_download_scout_act_requirement_follows_code_only_policy() -> None:
    assert download_scout_act_required_for_policy(BlockAuthoringPolicy.CODE_ONLY_BROWSER) is True
    assert download_scout_act_required_for_policy("code_only_browser") is True
    assert download_scout_act_required_for_policy(None) is False
    assert download_scout_act_required_for_policy(BlockAuthoringPolicy.STANDARD) is False


def test_base_agent_function_honors_code_block_mode_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_CODE_BLOCK_MODE", True)

    config = AgentFunction().get_copilot_config()

    assert config is not None
    assert config.block_authoring_policy == BlockAuthoringPolicy.CODE_ONLY_BROWSER


@pytest.mark.asyncio
async def test_base_agent_function_request_config_uses_env_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_CODE_BLOCK_MODE", True)

    config = await AgentFunction().get_copilot_config_for_request("o_test")

    assert config is not None
    assert config.block_authoring_policy == BlockAuthoringPolicy.CODE_ONLY_BROWSER
