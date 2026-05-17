from typing import Literal, cast

from pydantic import BaseModel

from skyvern.config import settings
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router

BrowserStreamingMode = Literal["cdp", "vnc"]
_ALLOWED_STREAMING_MODES: set[str] = {"cdp", "vnc"}


class RuntimeConfig(BaseModel):
    browser_streaming_mode: BrowserStreamingMode
    browser_streaming_label: str
    environment: str
    warnings: list[str] = []


def _normalize_browser_streaming_mode(value: str | None) -> tuple[BrowserStreamingMode, list[str]]:
    mode = (value or "").strip().lower()
    if mode in _ALLOWED_STREAMING_MODES:
        return cast(BrowserStreamingMode, mode), []
    return "vnc", [f"Invalid BROWSER_STREAMING_MODE={value!r}; using vnc fallback"]


def _browser_streaming_label(mode: BrowserStreamingMode) -> str:
    if mode == "cdp":
        return "Local browser streaming"
    return "VNC streaming"


@base_router.get("/config/runtime", include_in_schema=False)
@legacy_base_router.get("/config/runtime", include_in_schema=False)
async def get_runtime_config() -> RuntimeConfig:
    mode, warnings = _normalize_browser_streaming_mode(settings.BROWSER_STREAMING_MODE)
    return RuntimeConfig(
        browser_streaming_mode=mode,
        browser_streaming_label=_browser_streaming_label(mode),
        environment=settings.ENV,
        warnings=warnings,
    )
