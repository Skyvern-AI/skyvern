from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
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


def test_stdlib_extra_fields_reach_rendered_output() -> None:
    # Modules the codeblock-runner image imports cannot use structlog, so `extra=`
    # is their only route to structured fields. Without ExtraAdder in the
    # ProcessorFormatter's foreign_pre_chain these are silently dropped and never
    # become queryable attributes.
    rendered = _render(
        _foreign_record(
            runner_target="127.0.0.1:7819",
            unavailable_cause="grpc_unavailable",
            grpc_status="UNAVAILABLE",
        )
    )

    assert "runner_target" in rendered
    assert "127.0.0.1:7819" in rendered
    assert "unavailable_cause" in rendered
    assert "grpc_unavailable" in rendered
    assert "grpc_status" in rendered


def test_stdlib_record_without_extra_still_renders() -> None:
    rendered = _render(_foreign_record())

    assert "CodeBlock runner unavailable" in rendered


def test_json_foreign_record_keeps_its_message_alongside_extra_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    # The two tests above pass under the console renderer whether or not the JSON branch
    # works. In production this record arrived with an empty message and the text
    # stranded under `event`, because Datadog's remapper reads `msg`.
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


def test_json_foreign_record_carries_organization_id_from_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # The runner-unavailable alert groups by organization_id. A foreign record cannot bind
    # structlog context itself, so it has to be stamped from the ambient SkyvernContext.
    skyvern_context.set(SkyvernContext(organization_id="o_test_organization"))

    payload = _render_json(_foreign_record(unavailable_cause="grpc_unavailable"), monkeypatch)

    assert payload["organization_id"] == "o_test_organization"
