import asyncio
import hashlib
import mimetypes
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp
import structlog
from multidict import CIMultiDictProxy

from skyvern.config import settings
from skyvern.constants import BROWSER_DOWNLOAD_TIMEOUT, BROWSER_DOWNLOADING_SUFFIX, REPO_ROOT_DIR
from skyvern.exceptions import DownloadFileMaxSizeExceeded, DownloadFileMaxWaitingTime
from skyvern.forge.sdk.api.aws import AsyncAWSClient

LOG = structlog.get_logger()


async def download_from_s3(client: AsyncAWSClient, s3_uri: str) -> str:
    downloaded_bytes = await client.download_file(uri=s3_uri)
    filename = s3_uri.split("/")[-1]  # Extract filename from the end of S3 URI
    file_path = create_named_temporary_file(delete=False, file_name=filename)
    LOG.info(f"Downloaded file to {file_path.name}")
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


def extract_google_drive_file_id(url: str) -> str | None:
    """Extract file ID from Google Drive URL."""
    # Handle format: https://drive.google.com/file/d/{file_id}/view
    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    return None


async def download_file(url: str, max_size_mb: int | None = None) -> str:
    try:
        # Check if URL is a Google Drive link
        if "drive.google.com" in url:
            file_id = extract_google_drive_file_id(url)
            if file_id:
                # Convert to direct download URL
                url = f"https://drive.google.com/uc?export=download&id={file_id}"
                LOG.info("Converting Google Drive link to direct download", url=url)

        # Check if URL is an S3 URI
        if url.startswith(f"s3://{settings.AWS_S3_BUCKET_UPLOADS}/{settings.ENV}/o_"):
            LOG.info("Downloading Skyvern file from S3", url=url)
            client = AsyncAWSClient()
            return await download_from_s3(client, url)

        # Check if URL is a file:// URI
        # we only support to download local files when the environment is local
        # and the file is in the skyvern downloads directory
        if url.startswith("file://") and settings.ENV == "local":
            file_path = parse_uri_to_path(url)
            if file_path.startswith(f"{REPO_ROOT_DIR}/downloads"):
                LOG.info("Downloading file from local file system", url=url)
                return file_path

        async with aiohttp.ClientSession(raise_for_status=True) as session:
            LOG.info("Starting to download file", url=url)
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
    return Path(get_download_dir(workflow_run_id=workflow_run_id, task_id=None))


def get_download_dir(workflow_run_id: str | None, task_id: str | None) -> str:
    download_dir = f"{REPO_ROOT_DIR}/downloads/{workflow_run_id or task_id}"
    os.makedirs(download_dir, exist_ok=True)
    return download_dir


def list_files_in_directory(directory: Path, recursive: bool = False) -> list[str]:
    listed_files: list[str] = []
    for root, dirs, files in os.walk(directory):
        listed_files.extend([os.path.join(root, file) for file in files])
        if not recursive:
            break

    return listed_files


def list_downloading_files_in_directory(
    directory: Path, downloading_suffix: str = BROWSER_DOWNLOADING_SUFFIX
) -> list[Path]:
    # check if there's any file is still downloading
    downloading_files: list[Path] = []
    for file in list_files_in_directory(directory):
        path = Path(file)
        if path.suffix == downloading_suffix:
            downloading_files.append(path)
    return downloading_files


async def wait_for_download_finished(downloading_files: list[Path], timeout: float = BROWSER_DOWNLOAD_TIMEOUT) -> None:
    cur_downloading_files = downloading_files
    try:
        async with asyncio.timeout(timeout):
            while len(cur_downloading_files) > 0:
                new_downloading_files: list[Path] = []
                for path in cur_downloading_files:
                    if not path.exists():
                        continue
                    new_downloading_files.append(path)
                cur_downloading_files = new_downloading_files
                await asyncio.sleep(1)
    except asyncio.TimeoutError:
        raise DownloadFileMaxWaitingTime(downloading_files=cur_downloading_files)


def get_number_of_files_in_directory(directory: Path, recursive: bool = False) -> int:
    return len(list_files_in_directory(directory, recursive))


def sanitize_filename(filename: str) -> str:
    return "".join(c for c in filename if c.isalnum() or c in ["-", "_", ".", "%", " "])


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
    temp_dir = settings.TEMP_PATH
    create_folder_if_not_exist(temp_dir)
    return temp_dir


def make_temp_directory(
    suffix: str | None = None,
    prefix: str | None = None,
) -> str:
    temp_dir = settings.TEMP_PATH
    create_folder_if_not_exist(temp_dir)
    return tempfile.mkdtemp(suffix=suffix, prefix=prefix, dir=temp_dir)


def create_named_temporary_file(delete: bool = True, file_name: str | None = None) -> tempfile._TemporaryFileWrapper:
    temp_dir = settings.TEMP_PATH
    create_folder_if_not_exist(temp_dir)

    if file_name:
        # Sanitize the filename to remove any dangerous characters
        safe_file_name = sanitize_filename(file_name)
        # Create file with exact name (without random characters)
        file_path = os.path.join(temp_dir, safe_file_name)
        # Open in binary mode and return a NamedTemporaryFile-like object
        file = open(file_path, "wb")
        return tempfile._TemporaryFileWrapper(file, file_path, delete=delete)

    return tempfile.NamedTemporaryFile(dir=temp_dir, delete=delete)


def clean_up_dir(dir: str) -> None:
    if not os.path.exists(dir):
        return

    if os.path.isfile(dir):
        os.unlink(dir)
        return

    for item in os.listdir(dir):
        item_path = os.path.join(dir, item)
        if os.path.isfile(item_path) or os.path.islink(item_path):
            os.unlink(item_path)
        elif os.path.isdir(item_path):
            shutil.rmtree(item_path)

    return


def clean_up_skyvern_temp_dir() -> None:
    return clean_up_dir(get_skyvern_temp_dir())


def parse_uri_to_path(uri: str) -> str:
    parsed_uri = urlparse(uri)
    if parsed_uri.scheme != "file":
        raise ValueError(f"Invalid URI scheme: {parsed_uri.scheme} expected: file")
    path = parsed_uri.netloc + parsed_uri.path
    return unquote(path)
