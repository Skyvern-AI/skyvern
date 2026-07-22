"""Tests for inter_action_delay in the wait config experiment framework."""

import pytest

from skyvern.experimentation import wait_utils
from skyvern.experimentation.wait_config import WAIT_VARIANTS, WaitConfig
from skyvern.experimentation.wait_utils import get_wait_time
from skyvern.forge import app


def test_inter_action_delay_in_all_variants() -> None:
    for variant_name, config in WAIT_VARIANTS.items():
        assert "inter_action_delay" in config, f"Missing inter_action_delay in {variant_name}"
        assert isinstance(config["inter_action_delay"], (int, float))
        assert config["inter_action_delay"] >= 0.0


def test_baseline_preserves_current_behavior() -> None:
    assert WAIT_VARIANTS["baseline"]["inter_action_delay"] == 0.5


def test_aggressive_variant_zero_delay() -> None:
    assert WAIT_VARIANTS["aggressive"]["inter_action_delay"] == 0.0


def test_moderate_variant_reduced_delay() -> None:
    delay = WAIT_VARIANTS["moderate"]["inter_action_delay"]
    assert 0.0 < delay < WAIT_VARIANTS["baseline"]["inter_action_delay"]


def test_wait_config_returns_inter_action_delay() -> None:
    for variant_name in WAIT_VARIANTS:
        config = WaitConfig({"variant": variant_name})
        result = config.get_wait_time("inter_action_delay")
        assert result == WAIT_VARIANTS[variant_name]["inter_action_delay"]


def test_wait_config_override_inter_action_delay() -> None:
    config = WaitConfig({"variant": "baseline", "overrides": {"inter_action_delay": 0.2}})
    assert config.get_wait_time("inter_action_delay") == 0.2


def test_wait_config_global_multiplier() -> None:
    config = WaitConfig({"variant": "baseline", "global_multiplier": 2.0})
    assert config.get_wait_time("inter_action_delay") == 1.0


def test_wait_config_default_fallback() -> None:
    result = get_wait_time(None, "inter_action_delay", default=0.5)
    assert result == 0.5


@pytest.mark.asyncio
async def test_wait_config_is_none_when_killswitch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch the seam on the live app.AGENT_FUNCTION instance so this pins wait_utils'
    # branch on it, independent of which AgentFunction (or test stub) the app holds.
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_wait_time_optimization_enabled", lambda: False, raising=False)

    async def _must_not_run(cache_key: str, organization_id: str) -> None:
        raise AssertionError("experiment lookup must not run when the killswitch is off")

    monkeypatch.setattr(wait_utils, "get_wait_config_from_experiment", _must_not_run)
    assert await wait_utils.get_or_create_wait_config("tsk_kill_1", None, "org_1") is None


@pytest.mark.asyncio
async def test_wait_config_lookup_runs_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_wait_time_optimization_enabled", lambda: True, raising=False)
    calls: list[str] = []

    async def _fake_lookup(cache_key: str, organization_id: str) -> None:
        calls.append(cache_key)
        return None

    monkeypatch.setattr(wait_utils, "get_wait_config_from_experiment", _fake_lookup)
    wait_utils._wait_config_cache.clear()
    await wait_utils.get_or_create_wait_config("tsk_enabled_1", None, "org_1")
    assert calls == ["tsk_enabled_1"]
