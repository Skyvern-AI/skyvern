from typing import Any

import structlog

from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()

SCREENSHOT_DOWNSCALE_FLAG = "screenshot_downscale"

_DISABLED_FLAG_VALUES = {"control", "off", "false", "0", "disabled", "no"}
_ENABLED_FLAG_VALUES = {"treatment", "on", "true", "enabled", "yes"}


def _setting_default_height() -> int | None:
    settings = SettingsManager.get_settings()
    return settings.SCREENSHOT_DOWNSCALE_MAX_HEIGHT if settings.SCREENSHOT_DOWNSCALE_ENABLED else None


def _height_from_flag(flag_value: str | None, default_height: int | None) -> int | None:
    if not isinstance(flag_value, str):
        return default_height
    value = flag_value.strip().lower()
    if value in _DISABLED_FLAG_VALUES:
        return None
    if value in _ENABLED_FLAG_VALUES:
        return SettingsManager.get_settings().SCREENSHOT_DOWNSCALE_MAX_HEIGHT
    if value.isdigit():
        height = int(value)
        return height if height > 0 else None
    return default_height


def effective_downscale_height(context: SkyvernContext | None) -> int | None:
    """Resolved downscale height (None = skip): the per-run flag wins, else the static setting."""
    if context is not None and context.screenshot_downscale_max_height is not None:
        return context.screenshot_downscale_max_height
    return _setting_default_height()


async def resolve_screenshot_downscale_for_context(
    context: SkyvernContext,
    distinct_id: str,
    organization_id: str | None,
    *,
    workflow_permanent_id: str | None = None,
    task_url: str | None = None,
    log_context: dict[str, Any] | None = None,
) -> None:
    # Lazy import breaks an import cycle: this module is imported by webeye.utils.page, which
    # sits below the forge app singleton.
    from skyvern.forge import app

    default_height = _setting_default_height()
    properties: dict[str, str] = {}
    if organization_id:
        properties["organization_id"] = organization_id
    if workflow_permanent_id:
        properties["workflow_permanent_id"] = workflow_permanent_id
    if task_url:
        properties["task_url"] = task_url

    try:
        flag_value = await app.EXPERIMENTATION_PROVIDER.get_value_cached(
            SCREENSHOT_DOWNSCALE_FLAG,
            distinct_id,
            properties=properties,
        )
    except Exception:
        LOG.warning(
            "Failed to check screenshot_downscale feature flag",
            exc_info=True,
            distinct_id=distinct_id,
            **(log_context or {}),
        )
        context.screenshot_downscale_max_height = default_height
        return

    context.screenshot_downscale_variant = flag_value if isinstance(flag_value, str) else None
    context.screenshot_downscale_max_height = _height_from_flag(flag_value, default_height)
