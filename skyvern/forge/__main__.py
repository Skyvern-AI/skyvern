import os
import sys
from pathlib import Path

import structlog
from dotenv import load_dotenv
from uvicorn.config import Config as _UvicornConfig
from uvicorn.supervisors.watchfilesreload import FileFilter as _FileFilter

from skyvern import analytics
from skyvern.config import settings
from skyvern.exceptions import require_server_extra_modules

LOG = structlog.stdlib.get_logger()


def _build_reload_excludes() -> list[str]:
    # Pass directories so uvicorn's FileFilter uses depth-independent parents-check;
    # mkdir matters because is_dir() returns False on missing paths and demotes to glob.
    excludes: list[str] = []
    for raw_dir in (settings.TEMP_PATH, settings.ARTIFACT_STORAGE_PATH):
        if not raw_dir:
            continue
        abs_dir = Path(raw_dir).resolve()
        abs_dir.mkdir(parents=True, exist_ok=True)
        excludes.append(str(abs_dir))
    return excludes


def _verify_reload_excludes_cover_artifacts(reload_excludes: list[str]) -> None:
    # A miss lets watchfiles restart on artifact writes during long browser cleanups,
    # deadlocking the supervisor on Process.join.
    if not settings.ARTIFACT_STORAGE_PATH:
        return

    sample_artifact = (
        Path(settings.ARTIFACT_STORAGE_PATH).resolve() / "local" / "o_sample" / "scripts" / "s_sample" / "1" / "main.py"
    )
    cfg = _UvicornConfig(
        "skyvern.forge.api_app:create_api_app",
        reload=True,
        reload_excludes=reload_excludes,
        log_config=None,
    )
    # Outside the watched tree → watchfiles never sees writes here; exclude is moot.
    if not any(d in sample_artifact.parents for d in cfg.reload_dirs):
        return
    if _FileFilter(cfg)(sample_artifact):
        LOG.warning(
            "reload excludes do not cover a representative artifact path; "
            "long browser cleanups can wedge the uvicorn reload supervisor",
            sample_artifact=str(sample_artifact),
            reload_excludes=reload_excludes,
        )


if __name__ == "__main__":
    require_server_extra_modules("skyvern.forge", ("uvicorn",))

    import uvicorn

    from skyvern.forge.forge_app_initializer import _ensure_server_logging_configured

    _ensure_server_logging_configured()
    analytics.capture("skyvern-oss-run-server")
    port = settings.PORT
    LOG.info("Agent server starting.", host="0.0.0.0", port=port)
    load_dotenv()

    # Disable reload on Windows: uvicorn forces WindowsSelectorEventLoopPolicy when reload=True,
    # but Windows needs WindowsProactorEventLoopPolicy for async subprocess operations.
    disable_reload = os.getenv("SKYVERN_DEV_NO_RELOAD", "").lower() in ("1", "true", "yes")
    reload = settings.ENV == "local" and sys.platform != "win32" and not disable_reload

    reload_excludes: list[str] = []
    if reload:
        reload_excludes = _build_reload_excludes()
        _verify_reload_excludes_cover_artifacts(reload_excludes)

    uvicorn.run(
        "skyvern.forge.api_app:create_api_app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=False,
        # uvicorn's default LOGGING_CONFIG runs dictConfig() which resets the
        # uvicorn.error / uvicorn.access levels we set in setup_logger() back to
        # INFO, leaking WebSocket "connection open" / "WebSocket [accepted]" spam
        # to stderr. Pass a no-op dict so structlog stays in charge.
        log_config={"version": 1, "disable_existing_loggers": False},
        reload=reload,
        reload_excludes=reload_excludes,
        factory=True,
        ws="websockets-sansio",
    )
