import structlog
import uvicorn
from dotenv import load_dotenv

from skyvern import analytics
from skyvern.config import settings
from skyvern.forge.forge_uvicorn import create_uvicorn_config

LOG = structlog.stdlib.get_logger()

if __name__ == "__main__":
    analytics.capture("skyvern-oss-run-server")
    port = settings.PORT
    LOG.info("Agent server starting.", host="0.0.0.0", port=port)
    load_dotenv()

    uvicorn_config = create_uvicorn_config(
        app="skyvern.forge.api_app:app",
        port=port,
    )
    server = uvicorn.Server(uvicorn_config)
    server.run()
