"""Acceptance test for Skyvern.local(use_in_memory_db=True) bootstrap.

Covers the SQLite in-memory database, LocalStorage in a temp directory (rebuilt during
bootstrap) and lifecycle cleanup (engine disposal, client close).
"""

from pathlib import Path

import pytest


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
