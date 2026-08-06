from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from skyvern.webeye.video_utils import PreparedRecordingUpload


@asynccontextmanager
async def fake_prepared_recording(
    _path: str, prepared_path: str, extension: str = "mp4"
) -> AsyncIterator[PreparedRecordingUpload]:
    yield PreparedRecordingUpload(path=prepared_path, file_extension=extension)
