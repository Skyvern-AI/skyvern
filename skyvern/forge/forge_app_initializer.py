import structlog

from skyvern.config import settings
from skyvern.forge import set_force_app_instance
from skyvern.forge.forge_app import ForgeApp, create_forge_app

LOG = structlog.get_logger()


def start_forge_app() -> ForgeApp:
    force_app_instance = create_forge_app()
    set_force_app_instance(force_app_instance)

    if settings.ADDITIONAL_MODULES:
        for module in settings.ADDITIONAL_MODULES:
            LOG.info("Loading additional module to set up api app", module=module)
            app_module = __import__(module)
            configure_app_fn = getattr(app_module, "configure_app", None)
            if not configure_app_fn:
                raise RuntimeError(f"Missing configure_app function in {module}")

            configure_app_fn(force_app_instance)
        LOG.info(
            "Additional modules loaded to set up api app",
            modules=settings.ADDITIONAL_MODULES,
        )

    return force_app_instance
