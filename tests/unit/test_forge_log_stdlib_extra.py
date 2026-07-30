from __future__ import annotations

import logging

from skyvern.forge.sdk.forge_log import setup_logger


def _render(record: logging.LogRecord) -> str:
    setup_logger()
    formatter = logging.getLogger().handlers[0].formatter
    assert formatter is not None
    return formatter.format(record)


def _foreign_record(**extra: str) -> logging.LogRecord:
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
