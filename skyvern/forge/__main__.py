import structlog
import uvicorn
from dotenv import load_dotenv

import skyvern.forge.sdk.forge_log as forge_log
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.stdlib.get_logger()


if __name__ == "__main__":
    forge_log.setup_logger()
    port = SettingsManager.get_settings().PORT
    LOG.info("Agent server starting.", host="0.0.0.0", port=port)
    load_dotenv()

    reload = SettingsManager.get_settings().ENV == "local"
    uvicorn.run("skyvern.forge.app:app", host="0.0.0.0", port=port, log_level="info", reload=reload)
