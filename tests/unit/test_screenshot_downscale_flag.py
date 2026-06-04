import pytest

from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.experimentation.screenshot_downscale import (
    _height_from_flag,
    effective_downscale_height,
    resolve_screenshot_downscale_for_context,
)
from skyvern.forge.sdk.settings_manager import SettingsManager


def test_height_from_flag_absent_falls_back_to_default() -> None:
    assert _height_from_flag(None, 768) == 768
    assert _height_from_flag(None, None) is None


@pytest.mark.parametrize("value", ["control", "off", "false", "0", "no", "CONTROL"])
def test_height_from_flag_disabled_values_return_none(value: str) -> None:
    assert _height_from_flag(value, 768) is None


@pytest.mark.parametrize("value", ["treatment", "on", "true", "yes", "ENABLED"])
def test_height_from_flag_enabled_uses_setting_height(value: str) -> None:
    assert _height_from_flag(value, None) == SettingsManager.get_settings().SCREENSHOT_DOWNSCALE_MAX_HEIGHT


def test_height_from_flag_explicit_numeric_height() -> None:
    assert _height_from_flag("720", None) == 720
    assert _height_from_flag("1080", 768) == 1080


def test_height_from_flag_unknown_value_falls_back_to_default() -> None:
    assert _height_from_flag("garbage", 768) == 768


def test_effective_height_per_run_context_overrides_disabled_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SettingsManager.get_settings(), "SCREENSHOT_DOWNSCALE_ENABLED", False)
    assert effective_downscale_height(SkyvernContext(screenshot_downscale_max_height=720)) == 720


def test_effective_height_falls_back_to_setting_when_context_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SettingsManager.get_settings()
    monkeypatch.setattr(settings, "SCREENSHOT_DOWNSCALE_ENABLED", True)
    monkeypatch.setattr(settings, "SCREENSHOT_DOWNSCALE_MAX_HEIGHT", 768)
    assert effective_downscale_height(None) == 768
    assert effective_downscale_height(SkyvernContext()) == 768


def test_effective_height_disabled_with_no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SettingsManager.get_settings(), "SCREENSHOT_DOWNSCALE_ENABLED", False)
    assert effective_downscale_height(None) is None


class _FakeProvider:
    def __init__(self, value: str | None) -> None:
        self._value = value

    async def get_value_cached(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        return self._value


@pytest.mark.asyncio
async def test_resolver_treatment_records_variant_and_height(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge import app as forge_app

    monkeypatch.setattr(forge_app, "EXPERIMENTATION_PROVIDER", _FakeProvider("treatment"))
    ctx = SkyvernContext()
    await resolve_screenshot_downscale_for_context(ctx, "wr_test", "o_test")
    assert ctx.screenshot_downscale_variant == "treatment"
    assert ctx.screenshot_downscale_max_height == SettingsManager.get_settings().SCREENSHOT_DOWNSCALE_MAX_HEIGHT


@pytest.mark.asyncio
async def test_resolver_control_records_variant_no_downscale(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge import app as forge_app

    monkeypatch.setattr(forge_app, "EXPERIMENTATION_PROVIDER", _FakeProvider("control"))
    ctx = SkyvernContext()
    await resolve_screenshot_downscale_for_context(ctx, "wr_test", "o_test")
    assert ctx.screenshot_downscale_variant == "control"
    assert ctx.screenshot_downscale_max_height is None


@pytest.mark.asyncio
async def test_resolver_untargeted_run_has_null_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge import app as forge_app

    monkeypatch.setattr(forge_app, "EXPERIMENTATION_PROVIDER", _FakeProvider(None))
    monkeypatch.setattr(SettingsManager.get_settings(), "SCREENSHOT_DOWNSCALE_ENABLED", False)
    ctx = SkyvernContext()
    await resolve_screenshot_downscale_for_context(ctx, "wr_test", "o_test")
    assert ctx.screenshot_downscale_variant is None
    assert ctx.screenshot_downscale_max_height is None


def test_height_from_flag_non_string_is_defensive() -> None:
    # A provider returning a non-str (e.g. an unconfigured test mock) must not crash.
    assert _height_from_flag(object(), 768) == 768  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_resolver_tolerates_non_string_flag_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge import app as forge_app

    class _NonStringProvider:
        async def get_value_cached(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> object:
            return object()

    monkeypatch.setattr(forge_app, "EXPERIMENTATION_PROVIDER", _NonStringProvider())
    monkeypatch.setattr(SettingsManager.get_settings(), "SCREENSHOT_DOWNSCALE_ENABLED", False)
    ctx = SkyvernContext()
    await resolve_screenshot_downscale_for_context(ctx, "wr_test", "o_test")
    assert ctx.screenshot_downscale_variant is None
    assert ctx.screenshot_downscale_max_height is None
