"""Regression tests for the embedded mode bootstrap boundary.

These tests verify hermetic isolation between embedded mode and cloud modules.
They do NOT require an LLM key or Playwright — they test infrastructure only.
"""

import sys
from pathlib import Path

import pytest


def test_no_cloud_in_sys_modules() -> None:
    """Importing api_app must not trigger cloud/__init__.py in a clean process.

    In a mixed test suite where scenario conftest already imported cloud,
    this verifies the import itself doesn't RE-trigger cloud. In a clean
    process (SDK user), this verifies cloud is never loaded.
    """
    import importlib

    cloud_before = "cloud" in sys.modules
    if "skyvern.forge.api_app" in sys.modules:
        importlib.reload(sys.modules["skyvern.forge.api_app"])
    else:
        import skyvern.forge.api_app  # noqa: F401

    cloud_after = "cloud" in sys.modules
    if not cloud_before:
        assert not cloud_after, (
            f"cloud was loaded by importing api_app. Modules: {[k for k in sys.modules if k.startswith('cloud')]}"
        )


@pytest.mark.asyncio
async def test_bootstrap_creates_db() -> None:
    """Embedded bootstrap creates SQLite tables, org, and token."""
    from skyvern import Skyvern

    skyvern = Skyvern.local(use_in_memory_db=True)
    try:
        workflows = await skyvern.get_workflows()
        assert workflows is not None
    finally:
        await skyvern.aclose()


@pytest.mark.asyncio
async def test_artifact_tempdir_is_live() -> None:
    """StorageFactory points to an existing temp directory after bootstrap."""
    from skyvern import Skyvern
    from skyvern.library.embedded_server_factory import EmbeddedClient

    skyvern = Skyvern.local(use_in_memory_db=True)
    try:
        await skyvern.get_workflows()
        client = getattr(skyvern, "_embedded_client", None)
        assert isinstance(client, EmbeddedClient)
        artifact_dir = client.embedded_transport._artifact_dir
        assert artifact_dir is not None
        assert Path(artifact_dir).exists()
        assert "skyvern-artifacts-" in artifact_dir
    finally:
        await skyvern.aclose()


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
async def test_double_close_idempotent() -> None:
    """Calling aclose() twice does not raise."""
    from skyvern import Skyvern

    skyvern = Skyvern.local(use_in_memory_db=True)
    try:
        await skyvern.get_workflows()
    finally:
        await skyvern.aclose()
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
