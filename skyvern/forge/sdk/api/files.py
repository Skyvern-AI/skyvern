import os
import tempfile
import zipfile
from urllib.parse import urlparse

import requests
import structlog

LOG = structlog.get_logger()


def download_file(url: str) -> str | None:
    # Send an HTTP request to the URL of the file, stream=True to prevent loading the content at once into memory
    r = requests.get(url, stream=True)

    # Check if the request is successful
    if r.status_code == 200:
        # Parse the URL
        a = urlparse(url)

        # Get the file name
        temp_dir = tempfile.mkdtemp(prefix="skyvern_downloads_")

        file_name = os.path.basename(a.path)
        file_path = os.path.join(temp_dir, file_name)

        LOG.info(f"Downloading file to {file_path}")
        with open(file_path, "wb") as f:
            # Write the content of the request into the file
            for chunk in r.iter_content(1024):
                f.write(chunk)
        LOG.info(f"File downloaded successfully to {file_path}")
        return file_path
    else:
        LOG.error(f"Failed to download file, status code: {r.status_code}")
        return None


def zip_files(files_path: str, zip_file_path: str) -> str:
    with zipfile.ZipFile(zip_file_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(files_path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, files_path)  # Relative path within the zip
                zipf.write(file_path, arcname)

    return zip_file_path
