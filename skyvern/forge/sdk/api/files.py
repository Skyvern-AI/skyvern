import hashlib
import mimetypes
import os
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import structlog
from multidict import CIMultiDictProxy

from skyvern.constants import REPO_ROOT_DIR
from skyvern.exceptions import DownloadFileMaxSizeExceeded
from skyvern.forge.sdk.api.aws import AsyncAWSClient
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()


async def download_from_s3(client: AsyncAWSClient, s3_uri: str) -> str:
    downloaded_bytes = await client.download_file(uri=s3_uri)
    file_path = create_named_temporary_file(delete=False)
    file_path.write(downloaded_bytes)
    return file_path.name


def get_file_extension_from_headers(headers: CIMultiDictProxy[str]) -> str:
    # retrieve it from Content-Disposition
    content_disposition = headers.get("Content-Disposition")
    if content_disposition:
        filename = re.findall('filename="(.+)"', content_disposition, re.IGNORECASE)
        if len(filename) > 0 and Path(filename[0]).suffix:
            return Path(filename[0]).suffix

    # retrieve it from Content-Type
    content_type = headers.get("Content-Type")
    if content_type:
        if file_extension := mimetypes.guess_extension(content_type):
            return file_extension

    return ""


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
                temp_dir = make_temp_directory(prefix="skyvern_downloads_")

                file_name = os.path.basename(a.path)
                # if no suffix in the URL, we need to parse it from HTTP headers
                if not Path(file_name).suffix:
                    LOG.info("No file extension detected, trying to retrieve it from HTTP headers")
                    try:
                        if extension_name := get_file_extension_from_headers(response.headers):
                            file_name = file_name + extension_name
                        else:
                            LOG.warning("No extension name retreived from HTTP headers")
                    except Exception:
                        LOG.exception("Failed to retreive the file extension from HTTP headers")

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


def unzip_files(zip_file_path: str, output_dir: str) -> None:
    with zipfile.ZipFile(zip_file_path, "r") as zip_ref:
        zip_ref.extractall(output_dir)


def get_path_for_workflow_download_directory(workflow_run_id: str) -> Path:
    return Path(f"{REPO_ROOT_DIR}/downloads/{workflow_run_id}/")


def list_files_in_directory(directory: Path, recursive: bool = False) -> list[str]:
    listed_files: list[str] = []
    for root, dirs, files in os.walk(directory):
        listed_files.extend([os.path.join(root, file) for file in files])
        if not recursive:
            break

    return listed_files


def get_number_of_files_in_directory(directory: Path, recursive: bool = False) -> int:
    return len(list_files_in_directory(directory, recursive))


def sanitize_filename(filename: str) -> str:
    return "".join(c for c in filename if c.isalnum() or c in ["-", "_", "."])


def rename_file(file_path: str, new_file_name: str) -> str:
    try:
        new_file_name = sanitize_filename(new_file_name)
        new_file_path = os.path.join(os.path.dirname(file_path), new_file_name)
        os.rename(file_path, new_file_path)
        return new_file_path
    except Exception:
        LOG.exception(f"Failed to rename file {file_path} to {new_file_name}")
        return file_path


def calculate_sha256_for_file(file_path: str) -> str:
    """Helper function to calculate SHA256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def create_folder_if_not_exist(dir: str) -> None:
    path = Path(dir)
    if path.exists():
        return
    path.mkdir(parents=True)


def get_skyvern_temp_dir() -> str:
    temp_dir = SettingsManager.get_settings().TEMP_PATH
    create_folder_if_not_exist(temp_dir)
    return temp_dir


def make_temp_directory(
    suffix: str | None = None,
    prefix: str | None = None,
) -> str:
    temp_dir = SettingsManager.get_settings().TEMP_PATH
    create_folder_if_not_exist(temp_dir)
    return tempfile.mkdtemp(suffix=suffix, prefix=prefix, dir=temp_dir)


def create_named_temporary_file(delete: bool = True) -> tempfile._TemporaryFileWrapper:
    temp_dir = SettingsManager.get_settings().TEMP_PATH
    create_folder_if_not_exist(temp_dir)
    return tempfile.NamedTemporaryFile(dir=temp_dir, delete=delete)
