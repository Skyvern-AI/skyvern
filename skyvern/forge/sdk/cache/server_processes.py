import os

import structlog

LOG = structlog.get_logger()


def server_runs_one_process() -> bool:
    """uvicorn reads WEB_CONCURRENCY for its worker count; a value that is not a number counts as several."""
    try:
        return int(os.environ.get("WEB_CONCURRENCY") or 1) <= 1
    except ValueError:
        LOG.warning("WEB_CONCURRENCY is not a number; treating the server as several processes")
        return False
