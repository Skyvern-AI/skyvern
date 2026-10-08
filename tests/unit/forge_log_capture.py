from collections.abc import Iterator
from contextlib import contextmanager

import structlog
from structlog.testing import capture_logs

from skyvern.forge.sdk.forge_log import add_log_context, redact_registered_secrets, skyvern_logs_processor


@contextmanager
def capture_runtime_logs() -> Iterator[list[dict]]:
    config = structlog.get_config()
    processors = config["processors"]
    original_processors = processors.copy()
    try:
        with capture_logs() as logs:
            processors[:0] = [
                structlog.processors.EventRenamer("msg"),
                add_log_context,
                redact_registered_secrets,
                skyvern_logs_processor,
            ]
            yield logs
    finally:
        processors[:] = original_processors
        structlog.configure(**config)
