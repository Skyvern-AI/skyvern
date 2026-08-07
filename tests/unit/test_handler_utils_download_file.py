from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.actions import handler_utils


@pytest.mark.asyncio
async def test_download_file_accepts_run_local_absolute_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = "wr_upload"
    download_root = tmp_path / "downloads"
    run_dir = download_root / run_id
    run_dir.mkdir(parents=True)
    local_file = run_dir / "upload.csv"
    local_file.write_text("name\nAlice")

    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(download_root))
    monkeypatch.setattr(
        handler_utils.skyvern_context,
        "current",
        lambda: SkyvernContext(run_id=run_id, workflow_run_id=None, task_id=None),
    )
    download_mock = AsyncMock(side_effect=AssertionError("remote download must not be called"))
    monkeypatch.setattr(handler_utils, "download_file_api", download_mock)

    result = await handler_utils.download_file(str(local_file), action={"action_type": "upload_file"})

    assert result == str(local_file.resolve())
    download_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_file_absolute_path_outside_run_dir_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "wr_upload"
    download_root = tmp_path / "downloads"
    (download_root / run_id).mkdir(parents=True)
    outside_file = tmp_path / "outside.csv"
    outside_file.write_text("name\nAlice")

    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(download_root))
    monkeypatch.setattr(
        handler_utils.skyvern_context,
        "current",
        lambda: SkyvernContext(run_id=run_id, workflow_run_id=None, task_id=None),
    )
    download_mock = AsyncMock(side_effect=AssertionError("remote download must not be called"))
    monkeypatch.setattr(handler_utils, "download_file_api", download_mock)

    result = await handler_utils.download_file(str(outside_file), action={"action_type": "upload_file"})

    assert result == []
    download_mock.assert_not_awaited()
