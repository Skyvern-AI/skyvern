import os
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import structlog

from skyvern.constants import REPO_ROOT_DIR
from skyvern.exceptions import DownloadFileMaxSizeExceeded

LOG = structlog.get_logger()


async def download_file(url: str, max_size_mb: int | None = None) -> str:
    try:
        async with aiohttp.ClientSession(raise_for_status=True) as session:
            LOG.info("Starting to download file")
            async with session.get(url) as response:
                # Check the content length if available
                if max_size_mb and response.content_length and response.content_length > max_size_mb * 1024 * 1024:
                    # todo: move to root exception.py
                    raise DownloadFileMaxSizeExceeded(max_size_mb)

                # Parse the URL
                a = urlparse(url)

                # Get the file name
                temp_dir = tempfile.mkdtemp(prefix="skyvern_downloads_")

                file_name = os.path.basename(a.path)
                file_path = os.path.join(temp_dir, file_name)

                LOG.info(f"Downloading file to {file_path}")
                with open(file_path, "wb") as f:
                    # Write the content of the request into the file
                    total_bytes_downloaded = 0
                    async for chunk in response.content.iter_chunked(1024):
                        f.write(chunk)
                        total_bytes_downloaded += len(chunk)
                        if max_size_mb and total_bytes_downloaded > max_size_mb * 1024 * 1024:
                            raise DownloadFileMaxSizeExceeded(max_size_mb)

                LOG.info(f"File downloaded successfully to {file_path}")
                return file_path
    except aiohttp.ClientResponseError as e:
        LOG.error(f"Failed to download file, status code: {e.status}")
        raise
    except DownloadFileMaxSizeExceeded as e:
        LOG.error(f"Failed to download file, max size exceeded: {e.max_size}")
        raise
    except Exception:
        LOG.exception("Failed to download file")
        raise


def zip_files(files_path: str, zip_file_path: str) -> str:
    with zipfile.ZipFile(zip_file_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(files_path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, files_path)  # Relative path within the zip
                zipf.write(file_path, arcname)

    return zip_file_path


def get_path_for_workflow_download_directory(workflow_run_id: str) -> Path:
    return Path(f"{REPO_ROOT_DIR}/downloads/{workflow_run_id}/")


def get_number_of_files_in_directory(directory: Path, recursive: bool = False) -> int:
    count = 0
    for root, dirs, files in os.walk(directory):
        if not recursive:
            count += len(files)
            break
        count += len(files)
    return count
