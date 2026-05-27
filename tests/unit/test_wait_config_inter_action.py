"""Tests for inter_action_delay in the wait config experiment framework."""

from skyvern.experimentation.wait_config import WAIT_VARIANTS, WaitConfig
from skyvern.experimentation.wait_utils import get_wait_time


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
