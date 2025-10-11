from pathlib import Path
from typing import Optional

BACKEND_ENV_DEFAULT = ".env"
FRONTEND_DIRNAME = "skyvern-frontend"
FRONTEND_ENV_FILENAME = ".env"


def resolve_backend_env_path(
    basename: str = BACKEND_ENV_DEFAULT,
) -> Path:
    """Return the backend env file path inside the current working directory."""

    env_path = Path.cwd() / basename

    return env_path


def resolve_frontend_env_path() -> Optional[Path]:
    """Return the path to the frontend .env file (may not exist)."""

    frontend_root: Optional[Path] = None

    if frontend_root is None:
        cwd_frontend = Path.cwd() / FRONTEND_DIRNAME
        if cwd_frontend.exists() and cwd_frontend.is_dir():
            frontend_root = cwd_frontend

    if frontend_root is None:
        module_based_frontend = Path(__file__).resolve().parent.parent.parent / FRONTEND_DIRNAME
        if module_based_frontend.exists() and module_based_frontend.is_dir():
            frontend_root = module_based_frontend

    if frontend_root is None:
        for parent in Path.cwd().parents:
            candidate = parent / FRONTEND_DIRNAME
            if candidate.exists() and candidate.is_dir():
                frontend_root = candidate
                break

    if frontend_root is None:
        return None

    env_path = frontend_root / FRONTEND_ENV_FILENAME

    return env_path
