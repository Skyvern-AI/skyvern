import os
from enum import Enum
from pathlib import Path
from typing import Optional

BACKEND_ENV_DEFAULT = ".env"
BACKEND_ENV_BASENAMES = (BACKEND_ENV_DEFAULT, ".env.staging", ".env.prod")
BACKEND_ENV_DIRNAME = ".skyvern"
BACKEND_ENV_FILE_ENV_VAR = "SKYVERN_ENV_FILE"
BACKEND_ENV_INTENT_ENV_VAR = "SKYVERN_ENV_INTENT"
FRONTEND_DIRNAME = "skyvern-frontend"
FRONTEND_ENV_FILENAME = ".env"


class EnvScope(str, Enum):
    LEGACY = "legacy"
    PROJECT = "project"
    GLOBAL = "global"


class EnvIntent(str, Enum):
    AUTO = "auto"
    CLOUD = "cloud"
    LOCAL = "local"
    SERVER = "server"


_READ_SCOPE_ORDER: dict[EnvIntent, tuple[EnvScope, ...]] = {
    # Unscoped imports preserve legacy self-hosted behavior until a CLI command
    # selects a cloud/local intent explicitly.
    EnvIntent.AUTO: (EnvScope.LEGACY,),
    EnvIntent.CLOUD: (EnvScope.PROJECT, EnvScope.GLOBAL, EnvScope.LEGACY),
    # Reserved for embedded local SDK config writers; self-hosted CLI setup uses SERVER.
    EnvIntent.LOCAL: (EnvScope.PROJECT, EnvScope.LEGACY, EnvScope.GLOBAL),
    EnvIntent.SERVER: (EnvScope.LEGACY,),
}

_WRITE_SCOPE_DEFAULTS: dict[EnvIntent, EnvScope] = {
    EnvIntent.AUTO: EnvScope.LEGACY,
    EnvIntent.CLOUD: EnvScope.GLOBAL,
    EnvIntent.LOCAL: EnvScope.PROJECT,
    EnvIntent.SERVER: EnvScope.LEGACY,
}

_ENV_SCOPE_ALIASES = {
    "1": EnvScope.LEGACY,
    "legacy": EnvScope.LEGACY,
    "cwd": EnvScope.LEGACY,
    "current": EnvScope.LEGACY,
    "2": EnvScope.PROJECT,
    "project": EnvScope.PROJECT,
    "3": EnvScope.GLOBAL,
    "global": EnvScope.GLOBAL,
    "user": EnvScope.GLOBAL,
}


def _normalize_env_scope(scope: EnvScope | str | None) -> EnvScope | None:
    if scope is None:
        return None
    if isinstance(scope, EnvScope):
        return scope
    normalized = scope.strip().lower()
    choice = _ENV_SCOPE_ALIASES.get(normalized)
    if choice is not None:
        return choice
    raise ValueError("Choose one of: legacy/current, project, global, 1, 2, or 3.")


def _normalize_env_intent(intent: EnvIntent | str) -> EnvIntent:
    if isinstance(intent, EnvIntent):
        return intent
    return EnvIntent(intent.strip().lower())


def parse_env_scope(value: str) -> EnvScope:
    choice = _ENV_SCOPE_ALIASES.get(value.strip().lower())
    if choice is None:
        raise ValueError("Choose one of: legacy/current, project, global, 1, 2, or 3.")
    return choice


def _explicit_backend_env_path(basename: str) -> Path | None:
    explicit_path = os.getenv(BACKEND_ENV_FILE_ENV_VAR)
    if not explicit_path:
        return None

    env_path = Path(explicit_path).expanduser()
    if basename == BACKEND_ENV_DEFAULT:
        return env_path
    return env_path.parent / basename


def backend_env_path_for_scope(scope: EnvScope | str, basename: str = BACKEND_ENV_DEFAULT) -> Path:
    """Return the backend env path for an explicit storage scope."""
    normalized_scope = _normalize_env_scope(scope)
    if normalized_scope is EnvScope.LEGACY:
        return Path.cwd() / basename
    if normalized_scope is EnvScope.PROJECT:
        return Path.cwd() / BACKEND_ENV_DIRNAME / basename
    if normalized_scope is EnvScope.GLOBAL:
        return Path.home() / BACKEND_ENV_DIRNAME / basename
    raise ValueError(f"Unsupported env scope: {scope}")


def backend_env_path_candidates(
    basename: str = BACKEND_ENV_DEFAULT,
    *,
    intent: EnvIntent | str = EnvIntent.AUTO,
) -> tuple[Path, ...]:
    explicit_path = _explicit_backend_env_path(basename)
    if explicit_path is not None:
        return (explicit_path,)

    normalized_intent = _normalize_env_intent(intent)
    return tuple(backend_env_path_for_scope(scope, basename) for scope in _READ_SCOPE_ORDER[normalized_intent])


def _backend_env_load_candidates(intent: EnvIntent) -> tuple[Path, ...]:
    explicit_path = _explicit_backend_env_path(BACKEND_ENV_DEFAULT)
    if explicit_path is not None:
        explicit_paths: list[Path] = []
        for basename in BACKEND_ENV_BASENAMES:
            basename_path = _explicit_backend_env_path(basename)
            if basename_path is not None:
                explicit_paths.append(basename_path)
        return tuple(explicit_paths)

    paths: list[Path] = []
    for scope in reversed(_READ_SCOPE_ORDER[intent]):
        paths.extend(backend_env_path_for_scope(scope, basename) for basename in BACKEND_ENV_BASENAMES)
    return tuple(paths)


def load_backend_env_files(
    *,
    intent: EnvIntent | str = EnvIntent.AUTO,
    override: bool = False,
) -> Path:
    """Load backend env files for an intent and return the highest-priority path.

    Files are layered from lowest to highest priority so a project env can
    override global defaults, while real process env vars still win by default.
    This also records the selected intent in ``SKYVERN_ENV_INTENT`` so later
    settings imports use the same precedence.

    This mutates process-global environment state. CLI entrypoints should call
    it once, before importing ``skyvern.config`` or modules that import it, and
    avoid mixing cloud/server intent helpers in the same command.
    """
    from dotenv import dotenv_values  # noqa: PLC0415

    normalized_intent = _normalize_env_intent(intent)
    os.environ[BACKEND_ENV_INTENT_ENV_VAR] = normalized_intent.value

    values: dict[str, str] = {}
    for candidate in _backend_env_load_candidates(normalized_intent):
        if candidate.exists():
            values.update({key: value for key, value in dotenv_values(candidate).items() if value is not None})

    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value

    return resolve_backend_env_path(intent=normalized_intent)


def resolve_backend_env_path(
    basename: str = BACKEND_ENV_DEFAULT,
    *,
    intent: EnvIntent | str = EnvIntent.AUTO,
    scope: EnvScope | str | None = None,
    for_write: bool = False,
) -> Path:
    """Resolve the backend env file path.

    Resolution keeps source-checkout and existing CLI behavior compatible by
    preferring an existing ``./.env`` for reads. New flows can choose a scope
    explicitly, and ``SKYVERN_ENV_FILE`` remains the strongest override.
    """
    explicit_path = _explicit_backend_env_path(basename)
    if explicit_path is not None:
        return explicit_path

    normalized_scope = _normalize_env_scope(scope)
    if normalized_scope is not None:
        return backend_env_path_for_scope(normalized_scope, basename)

    normalized_intent = _normalize_env_intent(intent)
    if for_write:
        return backend_env_path_for_scope(_WRITE_SCOPE_DEFAULTS[normalized_intent], basename)

    for candidate in backend_env_path_candidates(basename, intent=normalized_intent):
        if candidate.exists():
            return candidate

    fallback_scope = _WRITE_SCOPE_DEFAULTS[normalized_intent]
    return backend_env_path_for_scope(fallback_scope, basename)


def env_scope_label(scope: EnvScope | str) -> str:
    normalized_scope = _normalize_env_scope(scope)
    if normalized_scope is EnvScope.LEGACY:
        return "Current directory (./.env)"
    if normalized_scope is EnvScope.PROJECT:
        return "Project directory (./.skyvern/.env)"
    if normalized_scope is EnvScope.GLOBAL:
        return "Global user directory (~/.skyvern/.env)"
    raise ValueError(f"Unsupported env scope: {scope}")


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
