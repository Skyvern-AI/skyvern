"""Tracing helpers for the workflow copilot OpenAI Agents SDK integration."""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Any

import structlog

LOG = structlog.get_logger()

_TRACING_ENABLED_VALUES = frozenset({"1", "true", "yes"})
_TRACING_INIT_LOCK = threading.Lock()
_TRACING_INITIALIZED = False


def is_tracing_enabled() -> bool:
    """Check COPILOT_TRACING_ENABLED env var (1/true/yes)."""
    value = os.getenv("COPILOT_TRACING_ENABLED", "")
    return value.strip().lower() in _TRACING_ENABLED_VALUES


def ensure_tracing_initialized() -> None:
    """Initialize Agents SDK tracing processors once.

    When ``COPILOT_TRACING_ENABLED`` is set, logfire is configured as the
    trace exporter.  Otherwise the SDK's built-in tracing to OpenAI servers
    is disabled to avoid 403/401 errors.
    """
    global _TRACING_INITIALIZED

    if _TRACING_INITIALIZED:
        return

    with _TRACING_INIT_LOCK:
        if _TRACING_INITIALIZED:
            return

        if not is_tracing_enabled():
            # Disable the SDK's built-in tracing to OpenAI servers.
            # Without this, the SDK attempts to upload traces and fails with
            # 403 for zero-data-retention orgs or 401 without an OpenAI key.
            try:
                from agents import set_tracing_disabled

                set_tracing_disabled(True)
            except ImportError:
                pass
            _TRACING_INITIALIZED = True
            return

        try:
            import logfire
        except ModuleNotFoundError:
            LOG.warning("Copilot tracing requested but logfire is not installed")
            _TRACING_INITIALIZED = True
            return

        logfire.configure(send_to_logfire="if-token-present", service_name="skyvern-copilot")
        logfire.instrument_openai_agents()
        # Logfire instruments via OpenTelemetry independently of the SDK's
        # built-in trace processors.  Clear the default OpenAI exporter so it
        # doesn't attempt to send traces (fails with 403 for ZDR orgs).
        try:
            from agents import set_trace_processors

            set_trace_processors([])
        except ImportError:
            pass
        _TRACING_INITIALIZED = True
        LOG.info("Initialized copilot tracing", exporter="logfire")


def copilot_span(name: str, data: dict[str, Any] | None = None) -> Any:
    """Return a tracing span context manager, or nullcontext() when tracing is off."""
    if not is_tracing_enabled():
        return contextlib.nullcontext()

    ensure_tracing_initialized()

    try:
        from agents.tracing import custom_span
    except ModuleNotFoundError as e:
        if e.name and e.name.startswith("agents"):
            return contextlib.nullcontext()
        raise

    return custom_span(name, data=data)
