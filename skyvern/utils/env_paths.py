import os
from pathlib import Path
from typing import Optional

from skyvern.constants import SKYVERN_DIR

BACKEND_ENV_DEFAULT = ".env"
FRONTEND_DIRNAME = "skyvern-frontend"
FRONTEND_ENV_FILENAME = ".env"
FRONTEND_ENV_OVERRIDE = "SKYVERN_FRONTEND_PATH"


def resolve_backend_env_path(
    basename: str = BACKEND_ENV_DEFAULT,
    *,
    create_if_missing: bool = False,
) -> Path:
    """Return the backend env file path inside the current working directory."""

    env_path = Path.cwd() / basename

    if create_if_missing and not env_path.exists():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.touch()

    return env_path


def resolve_frontend_env_path(create_if_missing: bool = False) -> Optional[Path]:
    """Return the path to the frontend .env file (may not exist)."""

    frontend_root: Optional[Path] = None

    override = os.environ.get(FRONTEND_ENV_OVERRIDE)
    if override:
        override_path = Path(override).expanduser().resolve()
        if override_path.exists():
            frontend_root = override_path

    if frontend_root is None:
        package_frontend = SKYVERN_DIR / FRONTEND_DIRNAME
        if package_frontend.exists():
            frontend_root = package_frontend

    if frontend_root is None:
        cwd_frontend = Path.cwd() / FRONTEND_DIRNAME
        if cwd_frontend.exists():
            frontend_root = cwd_frontend

    if frontend_root is None:
        for parent in Path.cwd().parents:
            candidate = parent / FRONTEND_DIRNAME
            if candidate.exists():
                frontend_root = candidate
                break

    if frontend_root is None:
        return None

    env_path = frontend_root / FRONTEND_ENV_FILENAME
    if create_if_missing and not env_path.exists():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.touch()

    return env_path
