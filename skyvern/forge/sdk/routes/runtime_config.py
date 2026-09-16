from typing import Literal, cast

from pydantic import BaseModel

from skyvern.config import CodeBlockMode, settings
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router

BrowserStreamingMode = Literal["cdp", "vnc"]
_ALLOWED_STREAMING_MODES: set[str] = {"cdp", "vnc"}
_SELF_HOSTED_ENVIRONMENTS = frozenset({"local", "selfhost", "selfhosted"})


class RuntimeConfig(BaseModel):
    browser_streaming_mode: BrowserStreamingMode
    browser_streaming_label: str
    environment: str
    workflow_copilot_code_block_mode: bool | None = None
    code_block_access: bool | None = None
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


@base_router.get("/config/runtime", include_in_schema=False, response_model_exclude_none=True)
@legacy_base_router.get("/config/runtime", include_in_schema=False, response_model_exclude_none=True)
async def get_runtime_config() -> RuntimeConfig:
    mode, warnings = _normalize_browser_streaming_mode(settings.BROWSER_STREAMING_MODE)
    expose_oss_code_config = settings.ENV in _SELF_HOSTED_ENVIRONMENTS
    return RuntimeConfig(
        browser_streaming_mode=mode,
        browser_streaming_label=_browser_streaming_label(mode),
        environment=settings.ENV,
        workflow_copilot_code_block_mode=(
            settings.WORKFLOW_COPILOT_CODE_BLOCK_MODE if expose_oss_code_config else None
        ),
        code_block_access=(settings.CODE_BLOCK_MODE is CodeBlockMode.enabled if expose_oss_code_config else None),
        warnings=warnings,
    )
