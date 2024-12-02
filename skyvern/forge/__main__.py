import structlog
import uvicorn
from dotenv import load_dotenv

from skyvern import analytics
from skyvern.config import settings

LOG = structlog.stdlib.get_logger()


if __name__ == "__main__":
    analytics.capture("skyvern-oss-run-server")
    port = settings.PORT
    LOG.info("Agent server starting.", host="0.0.0.0", port=port)
    load_dotenv()

    reload = settings.ENV == "local"
    uvicorn.run(
        "skyvern.forge.api_app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        reload=reload,
    )
