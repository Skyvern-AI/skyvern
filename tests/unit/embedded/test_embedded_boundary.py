"""Regression tests for the embedded mode bootstrap boundary.

They do NOT require an LLM key or Playwright — they test infrastructure only.
"""

import pytest


@pytest.mark.asyncio
async def test_unique_llm_key_no_collision() -> None:
    """Sequential clients (create -> close -> create again) use different LLM keys."""
    from skyvern import Skyvern
    from skyvern.forge.sdk.api.llm.models import LLMConfig

    config = LLMConfig(
        model_name="gpt-4o-mini",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )

    skyvern1 = Skyvern.local(use_in_memory_db=True, llm_config=config)
    try:
        await skyvern1.get_workflows()
    finally:
        await skyvern1.aclose()

    # Second client should NOT raise DuplicateLLMConfigError
    skyvern2 = Skyvern.local(use_in_memory_db=True, llm_config=config)
    try:
        await skyvern2.get_workflows()
    finally:
        await skyvern2.aclose()
    # aclose() must be idempotent.
    await skyvern2.aclose()


@pytest.mark.asyncio
async def test_second_client_fails_fast() -> None:
    """Creating a second embedded client WHILE the first is still open raises."""
    from skyvern import Skyvern

    skyvern1 = Skyvern.local(use_in_memory_db=True)
    try:
        with pytest.raises(RuntimeError, match="already active"):
            Skyvern.local(use_in_memory_db=True)
    finally:
        await skyvern1.aclose()


@pytest.mark.asyncio
async def test_close_without_bootstrap() -> None:
    """aclose() on a client that never made a request is a no-op."""
    from skyvern import Skyvern

    skyvern = Skyvern.local(use_in_memory_db=True)
    await skyvern.aclose()


@pytest.mark.asyncio
async def test_blocked_settings_rejected() -> None:
    """Attempting to override OTEL_ENABLED or ENABLE_CLEANUP_CRON raises ValueError."""
    from skyvern import Skyvern

    skyvern = Skyvern.local(use_in_memory_db=True, settings={"OTEL_ENABLED": True})
    try:
        # Validation happens on first request (lazy bootstrap)
        with pytest.raises(ValueError, match="Cannot override"):
            await skyvern.get_workflows()
    finally:
        await skyvern.aclose()
