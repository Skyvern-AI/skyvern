import pytest

from skyvern.config import CodeBlockMode, settings
from skyvern.forge.sdk.routes.runtime_config import get_runtime_config


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["local", "selfhost", "selfhosted"])
async def test_runtime_config_exposes_self_hosted_copilot_code_mode(monkeypatch, environment: str) -> None:
    monkeypatch.setattr(settings, "ENV", environment)
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_CODE_BLOCK_MODE", True)
    monkeypatch.setattr(settings, "CODE_BLOCK_MODE", CodeBlockMode.enabled)

    config = await get_runtime_config()

    assert config.workflow_copilot_code_block_mode is True
    assert config.code_block_access is True


@pytest.mark.asyncio
async def test_runtime_config_exposes_disabled_code_block_access(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ENV", "selfhosted")
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_CODE_BLOCK_MODE", True)
    monkeypatch.setattr(settings, "CODE_BLOCK_MODE", CodeBlockMode.disabled)

    config = await get_runtime_config()

    assert config.workflow_copilot_code_block_mode is True
    assert config.code_block_access is False


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["staging", "production", "eu-production", "preview"])
async def test_runtime_config_omits_code_execution_posture_in_skyvern_cloud(monkeypatch, environment: str) -> None:
    monkeypatch.setattr(settings, "ENV", environment)
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_CODE_BLOCK_MODE", True)
    monkeypatch.setattr(settings, "CODE_BLOCK_MODE", CodeBlockMode.enabled)

    config = await get_runtime_config()

    assert config.workflow_copilot_code_block_mode is None
    assert config.code_block_access is None
    assert "workflow_copilot_code_block_mode" not in config.model_dump(exclude_none=True)
    assert "code_block_access" not in config.model_dump(exclude_none=True)
