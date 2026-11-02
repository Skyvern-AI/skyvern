from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.forge_log import setup_logger


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """setup_logger() replaces the root handler process-wide, so JSON-mode tests would
    otherwise leak that handler into every test that runs after them."""
    yield
    skyvern_context.reset()
    setup_logger()


def _render(record: logging.LogRecord) -> str:
    setup_logger()
    formatter = logging.getLogger().handlers[0].formatter
    assert formatter is not None
    return formatter.format(record)


def _render_json(record: logging.LogRecord, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Render through the JSON branch — the one production and staging run."""
    monkeypatch.setattr(settings, "JSON_LOGGING", True)
    return json.loads(_render(record))


def _foreign_record(**extra: object) -> logging.LogRecord:
    """A stdlib record shaped like `LOG.warning(msg, extra={...})` produces."""
    record = logging.LogRecord(
        name="codeblock.codeblock_grpc",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="CodeBlock runner unavailable",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_foreign_record_keeps_its_message_alongside_extra_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    # In production this record arrived with an empty message and the text stranded
    # under `event`, because Datadog's remapper reads `msg`.
    payload = _render_json(
        _foreign_record(
            runner_target="127.0.0.1:7819",
            unavailable_cause="grpc_unavailable",
            grpc_status="UNAVAILABLE",
        ),
        monkeypatch,
    )

    assert payload.get("msg", "").startswith("CodeBlock runner unavailable")
    assert "event" not in payload
    assert payload["unavailable_cause"] == "grpc_unavailable"
    assert payload["runner_target"] == "127.0.0.1:7819"
    assert payload["grpc_status"] == "UNAVAILABLE"


def test_json_foreign_record_redacts_proxy_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _render_json(
        _foreign_record(
            proxy_location={"url": "http://user:synthetic-secret@token.proxy.example:8080"},
        ),
        monkeypatch,
    )
    rendered = json.dumps(payload)

    assert "synthetic-secret" not in rendered
    assert "token.proxy.example" not in rendered
    assert re.fullmatch(r"custom_url:[0-9a-f]{12}", payload["proxy_location"])
