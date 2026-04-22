"""End-to-end acceptance tests for Skyvern.local(use_in_memory_db=True).

Exercises the full embedded mode path:
- SQLite in-memory database
- BackgroundTaskExecutor (default in-process executor)
- LocalStorage with temp directory (rebuilt during bootstrap)
- Lifecycle cleanup (engine disposal, client close)

Requires OPENAI_API_KEY and Playwright browsers installed.
Skipped in CI unless the key is available.
"""

import os
from pathlib import Path

import pytest

from skyvern.forge.sdk.api.llm.models import LLMConfig


def _has_playwright_browser() -> bool:
    """Check that Playwright's chromium binary exists for the current installed version."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


_skip_no_llm_or_browser = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY") or not _has_playwright_browser(),
    reason="Requires OPENAI_API_KEY and Playwright browsers installed (run: playwright install chromium)",
)


@pytest.mark.asyncio
@_skip_no_llm_or_browser
async def test_embedded_mode_run_task() -> None:
    """Path 1: run_task() exercises BackgroundTaskExecutor + polling + full DB lifecycle.

    Flow: httpx -> ASGITransport -> FastAPI -> BackgroundTaskExecutor ->
          ForgeAgent.execute_step() -> LLM + browser -> artifact save -> task complete
    """
    from skyvern import Skyvern

    skyvern = Skyvern.local(
        use_in_memory_db=True,
        llm_config=LLMConfig(
            model_name="gpt-4o-mini",
            required_env_vars=["OPENAI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
        ),
    )

    try:
        result = await skyvern.run_task(
            prompt="Extract the page title",
            url="https://example.com",
            wait_for_completion=True,
            timeout=120,
        )
        assert result is not None
        assert result.status is not None
    finally:
        await skyvern.aclose()


@pytest.mark.asyncio
@_skip_no_llm_or_browser
async def test_embedded_mode_page_extract() -> None:
    """Path 2: page.extract() exercises direct in-process LLM + browser path.

    Flow: page.extract() -> run_sdk_action() -> LLM call -> result
    Does NOT go through BackgroundTaskExecutor or polling.
    """
    from skyvern import Skyvern

    skyvern = Skyvern.local(
        use_in_memory_db=True,
        llm_config=LLMConfig(
            model_name="gpt-4o-mini",
            required_env_vars=["OPENAI_API_KEY"],
            supports_vision=True,
            add_assistant_prefix=False,
        ),
    )

    try:
        browser = await skyvern.launch_local_browser()
        page = await browser.get_working_page()
        await page.goto("https://example.com")
        result = await page.extract("What is the title of this page?")
        assert result is not None
    finally:
        await skyvern.aclose()


@pytest.mark.asyncio
async def test_embedded_mode_bootstrap_and_artifacts() -> None:
    """Verify embedded bootstrap creates SQLite DB, org, token, and redirects artifacts to tempdir.

    Does NOT require an LLM key — tests the infrastructure, not the LLM path.
    Bootstrap is lazy (happens on first request), so we trigger it via get_workflows().
    """
    from skyvern import Skyvern

    skyvern = Skyvern.local(use_in_memory_db=True)

    try:
        workflows = await skyvern.get_workflows()
        assert workflows is not None

        embedded_client = getattr(skyvern, "_embedded_client", None)
        assert embedded_client is not None

        from skyvern.library.embedded_server_factory import EmbeddedClient

        assert isinstance(embedded_client, EmbeddedClient)
        transport = embedded_client.embedded_transport
        assert transport is not None

        artifact_dir = getattr(transport, "_artifact_dir", None)
        assert artifact_dir is not None
        assert Path(artifact_dir).exists()
        assert "skyvern-artifacts-" in artifact_dir
    finally:
        await skyvern.aclose()
