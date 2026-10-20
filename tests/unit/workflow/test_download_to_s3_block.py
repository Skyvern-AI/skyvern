from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.api import files
from skyvern.forge.sdk.workflow.models.block import DownloadToS3Block
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter


@pytest.mark.asyncio
@pytest.mark.parametrize("upload_fails", [False, True], ids=["success", "failure"])
async def test_download_to_s3_preserves_same_run_file_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upload_fails: bool
) -> None:
    run_id = "wr_test"
    path = tmp_path / "downloads" / run_id / "input.txt"
    path.parent.mkdir(parents=True)
    path.write_text("original")
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "downloads"))
    monkeypatch.setattr(settings, "ENV", "local")
    monkeypatch.setattr(files, "_storage_manages_file", lambda *_: False)

    workflow_run_context = MagicMock()
    workflow_run_context.has_parameter.return_value = False
    workflow_run_context.has_value.return_value = False

    class FakeS3Client:
        async def upload_file_from_path(self, *, uri: str, file_path: str) -> None:
            if upload_fails:
                raise RuntimeError("upload failed")

    block = DownloadToS3Block(
        label="download_to_s3",
        output_parameter=OutputParameter(
            output_parameter_id="op_test",
            workflow_id="w_test",
            key="uploaded_file",
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
        ),
        url=path.as_uri(),
    )
    monkeypatch.setattr(DownloadToS3Block, "get_workflow_run_context", lambda self, _: workflow_run_context)
    monkeypatch.setattr(DownloadToS3Block, "record_output_parameter_value", AsyncMock())
    monkeypatch.setattr(DownloadToS3Block, "get_async_aws_client", lambda self: FakeS3Client())
    monkeypatch.setattr("skyvern.forge.sdk.workflow.models.block.skyvern_context.current", lambda: None)

    if upload_fails:
        with pytest.raises(RuntimeError, match="upload failed"):
            await block.execute(run_id, "", organization_id="org_test")
    else:
        result = await block.execute(run_id, "", organization_id="org_test")
        assert result.success

    assert path.read_text() == "original"
