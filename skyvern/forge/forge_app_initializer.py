from threading import Lock

import structlog

from skyvern.config import settings
from skyvern.forge import set_force_app_instance
from skyvern.forge.forge_app import ForgeApp, create_forge_app
from skyvern.forge.sdk.artifact.storage.azure import AzureStorage
from skyvern.forge.sdk.artifact.storage.factory import StorageFactory
from skyvern.forge.sdk.artifact.storage.gcs import GcsStorage
from skyvern.forge.sdk.artifact.storage.s3 import S3Storage
from skyvern.forge.sdk.db.agent_db import AgentDB

LOG = structlog.get_logger()
_SERVER_LOGGING_CONFIGURED = False
_SERVER_LOGGING_LOCK = Lock()


def _ensure_server_logging_configured() -> None:
    global _SERVER_LOGGING_CONFIGURED
    with _SERVER_LOGGING_LOCK:
        if _SERVER_LOGGING_CONFIGURED:
            return

        from skyvern.forge.sdk.forge_log import setup_logger  # noqa: PLC0415

        setup_logger()
        _SERVER_LOGGING_CONFIGURED = True


def start_forge_app() -> ForgeApp:
    _ensure_server_logging_configured()

    force_app_instance = create_forge_app()
    set_force_app_instance(force_app_instance)

    if settings.ADDITIONAL_MODULES:
        for module in settings.ADDITIONAL_MODULES:
            LOG.debug("Loading additional module to set up api app", module=module)
            app_module = __import__(module)
            configure_app_fn = getattr(app_module, "configure_app", None)
            if not configure_app_fn:
                raise RuntimeError(f"Missing configure_app function in {module}")

            configure_app_fn(force_app_instance)
        LOG.debug(
            "Additional modules loaded to set up api app",
            modules=settings.ADDITIONAL_MODULES,
        )

    return force_app_instance


def start_streaming_worker_app() -> ForgeApp:
    """Initialize the minimal app graph the screenshot streaming worker needs.

    The all-in-one container runs ``run_streaming.py`` as a second process
    alongside the main server. That worker only reads run/task rows and writes
    screenshot files, so it needs ONLY ``app.DATABASE`` and ``app.STORAGE`` --
    not the full ``create_forge_app()`` object graph (browser manager, LLM
    clients/handlers, persistent-sessions manager, credential vaults, workflow
    service, agent, replica DB, cache, ...). Building only the proven-minimal
    bundle avoids duplicating that heavyweight fixed process state in a second
    process.

    A startup failure here (e.g. an unreachable database) propagates and fails
    the process closed, rather than being swallowed into a silent screenshot
    loop; ``set_force_app_instance`` runs only once the minimal bundle is built.
    """
    _ensure_server_logging_configured()

    force_app_instance = ForgeApp()
    force_app_instance.SETTINGS_MANAGER = settings
    force_app_instance.DATABASE = AgentDB(settings.DATABASE_STRING, debug_enabled=settings.DEBUG_MODE)

    # Storage backend selection mirrors create_forge_app(); keep the two in sync
    # so a new backend is never silently downgraded to LocalStorage in the worker.
    if settings.SKYVERN_STORAGE_TYPE == "s3":
        StorageFactory.set_storage(S3Storage())
    elif settings.SKYVERN_STORAGE_TYPE == "azureblob":
        StorageFactory.set_storage(AzureStorage())
    elif settings.SKYVERN_STORAGE_TYPE == "gcs":
        StorageFactory.set_storage(GcsStorage())
    force_app_instance.STORAGE = StorageFactory.get_storage()

    set_force_app_instance(force_app_instance)
    return force_app_instance
