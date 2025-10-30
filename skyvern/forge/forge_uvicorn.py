import os

import structlog
import uvicorn
from fastapi import FastAPI

from skyvern.config import settings

LOG = structlog.stdlib.get_logger()


def create_uvicorn_config(app: FastAPI | str, port: int | None = None, reload: bool | None = None) -> uvicorn.Config:
    """Create a uvicorn configuration for the Skyvern server.

    Args:
        app: FastAPI app instance or import string (e.g., "skyvern.forge.api_app:app")
        port: Port number to run the server on. Defaults to settings.PORT.
        reload: Whether to enable auto-reload. Defaults to True in local env, False otherwise.

    Returns:
        Configured uvicorn.Config instance.
    """
    if port is None:
        port = settings.PORT

    if reload is None:
        reload = settings.ENV == "local"

    # Configure reload settings
    # Convert TEMP_PATH to relative path if it's absolute to avoid pathlib.glob() issues
    temp_path_for_excludes = (
        os.path.relpath(settings.TEMP_PATH) if os.path.isabs(settings.TEMP_PATH) else settings.TEMP_PATH
    )
    artifact_path_for_excludes = (
        os.path.relpath(settings.ARTIFACT_STORAGE_PATH)
        if os.path.isabs(settings.ARTIFACT_STORAGE_PATH)
        else settings.ARTIFACT_STORAGE_PATH
    )

    return uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        reload=reload,
        reload_excludes=[
            f"{temp_path_for_excludes}/**/*.py",
            f"{artifact_path_for_excludes}/{settings.ENV}/**/scripts/**/**/*.py",
        ],
        access_log=False,
    )
