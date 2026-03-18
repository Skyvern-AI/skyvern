"""Tests for MiniMax model configuration in LLMConfigRegistry."""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture()
def minimax_registry():
    """Enable MiniMax and reload the config registry to get fresh registrations."""
    os.environ["ENABLE_MINIMAX"] = "true"
    os.environ["MINIMAX_API_KEY"] = "test-key"

    import skyvern.config as config_mod
    import skyvern.forge.sdk.api.llm.config_registry as reg_mod

    # Clear registry and reload to re-register with MiniMax enabled
    reg_mod.LLMConfigRegistry._configs = {}
    importlib.reload(config_mod)
    importlib.reload(reg_mod)

    yield reg_mod.LLMConfigRegistry

    # Cleanup
    os.environ.pop("ENABLE_MINIMAX", None)
    os.environ.pop("MINIMAX_API_KEY", None)
    reg_mod.LLMConfigRegistry._configs = {}
    importlib.reload(config_mod)
    importlib.reload(reg_mod)


def test_minimax_m27_registered(minimax_registry):
    """M2.7 model should be registered when MiniMax is enabled."""
    config = minimax_registry.get_config("MINIMAX_M2_7")
    assert "MiniMax-M2.7" in config.model_name


def test_minimax_m27_highspeed_registered(minimax_registry):
    """M2.7-highspeed model should be registered when MiniMax is enabled."""
    config = minimax_registry.get_config("MINIMAX_M2_7_HIGHSPEED")
    assert "MiniMax-M2.7-highspeed" in config.model_name


def test_minimax_m25_still_available(minimax_registry):
    """Previous M2.5 models should still be available."""
    assert "MINIMAX_M2_5" in minimax_registry.get_model_names()
    assert "MINIMAX_M2_5_HIGHSPEED" in minimax_registry.get_model_names()


def test_minimax_m27_supports_vision(minimax_registry):
    """M2.7 model should support vision."""
    config = minimax_registry.get_config("MINIMAX_M2_7")
    assert config.supports_vision is True
