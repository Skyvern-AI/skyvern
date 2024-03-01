import asyncio
import random
import string
import typing
from typing import Any, Callable

from PIL import Image

from skyvern.forge.sdk.api.aws import AsyncAWSClient

async_s3_client = AsyncAWSClient()


def read_artifact(uri: str, is_image: bool = False, is_webm: bool = False) -> Image.Image | str | bytes:
    """Load and display an artifact based on its URI."""
    if uri.startswith("s3://"):
        downloaded_bytes = asyncio.run(async_s3_client.download_file(uri))
        if is_image:
            return downloaded_bytes
        elif is_webm:
            return downloaded_bytes
        else:
            return downloaded_bytes.decode("utf-8")
    elif uri.startswith("file://"):
        # Remove file:// prefix
        uri = uri[7:]
        # Means it's a local file
        if is_image:
            with open(uri, "rb") as f:
                image = Image.open(f)
                image.load()
                return image
        elif is_webm:
            with open(uri, "rb") as f:
                return f.read()
        else:
            with open(uri, "r") as f:
                return f.read()
    else:
        raise ValueError(f"Unsupported URI: {uri}")


def read_artifact_safe(uri: str, is_image: bool = False, is_webm: bool = False) -> Image.Image | str | bytes:
    """Load and display an artifact based on its URI."""
    try:
        return read_artifact(uri, is_image, is_webm)
    except Exception as e:
        return f"Failed to load artifact: {e}"


def streamlit_content_safe(st_obj: Any, f: Callable, content: bytes, message: str, **kwargs: dict[str, Any]) -> None:
    try:
        if content:
            f(content, **kwargs)
        else:
            st_obj.write(message)
    except Exception:
        st_obj.write(message)


@typing.no_type_check
def streamlit_show_recording(st_obj: Any, uri: str) -> None:
    # ignoring type because is_webm will return bytes
    content = read_artifact_safe(uri, is_webm=True)  # type: ignore
    if content:
        random_key = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        st_obj.download_button("Download recording", content, f"recording{uri.split('/')[-1]}.webm", key=random_key)

    streamlit_content_safe(st_obj, st_obj.video, content, "No recording available.", format="video/webm", start_time=0)
