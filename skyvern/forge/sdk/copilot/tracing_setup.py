"""Tracing helpers for the workflow copilot OpenAI Agents SDK integration.

Tracing is dev-only and opt-in. See ``cloud_docs/local-dev/copilot-tracing.md``
for how to enable it without adding logfire to the project lockfile.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import threading
from typing import Any

import structlog

# Reuse the HTTP-logging redactor so trace-side and SSE-side redaction share
# one exact-match sensitive-key policy.
from skyvern.forge.request_logging import redact_sensitive_fields

LOG = structlog.get_logger()

_TRACING_ENABLED_VALUES = frozenset({"1", "true", "yes"})
_TRACING_INIT_LOCK = threading.Lock()
_TRACING_INITIALIZED = False
# Set the first time the per-span rename patch fails so the warning fires
# once per process. threading.Event.set() / is_set() are race-free without
# a hot-path lock -- just as cheap as a bool read in the happy (already-set)
# case.
_SPAN_RENAME_WARNED = threading.Event()

# Logfire private-internals path the patch reaches into. Kept here so it is
# easy to audit when a logfire upgrade surfaces the rename-warning.
_LOGFIRE_PRIVATE_MODULE = "logfire._internal.integrations.openai_agents"
_LOGFIRE_PATCH_SYMBOLS = ("attributes_from_span_data", "LogfireTraceProviderWrapper")

# Set by agent.py before running the agent so the span patch can read it.
_copilot_model_name: contextvars.ContextVar[str | None] = contextvars.ContextVar("_copilot_model_name", default=None)


def is_tracing_enabled() -> bool:
    """Check COPILOT_TRACING_ENABLED env var (1/true/yes)."""
    value = os.getenv("COPILOT_TRACING_ENABLED", "")
    return value.strip().lower() in _TRACING_ENABLED_VALUES


def _clear_sdk_trace_processors() -> None:
    """Clear the SDK's built-in trace processors to prevent uploads to OpenAI."""
    try:
        from agents import set_trace_processors

        set_trace_processors([])
    except ImportError:
        pass


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
            _clear_sdk_trace_processors()
            _TRACING_INITIALIZED = True
            return

        logfire.configure(send_to_logfire="if-token-present", service_name="skyvern-copilot")
        logfire.instrument_openai_agents()
        _patch_agent_span_attributes()
        # Logfire instruments via OpenTelemetry independently of the SDK's
        # built-in trace processors.  Clear the default OpenAI exporter so it
        # doesn't attempt to send traces (fails with 403 for ZDR orgs).
        _clear_sdk_trace_processors()
        _TRACING_INITIALIZED = True
        LOG.info("Initialized copilot tracing", exporter="logfire")


def _patch_agent_span_attributes() -> None:
    """Patch logfire to emit OTel GenAI semantic convention attributes on agent spans.

    Logfire's OpenAI agents integration puts the agent name under a plain
    ``name`` attribute but the Agents dashboard requires the OTel semantic
    convention attributes per:

    * https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/
    * https://opentelemetry.io/docs/specs/semconv/gen-ai/openai/

    Required for ``invoke_agent``:
    * ``gen_ai.agent.name``
    * ``gen_ai.operation.name`` = ``invoke_agent``
    * ``gen_ai.provider.name`` = ``openai``
    * ``gen_ai.request.model`` (read from ``_copilot_model_name`` context var)

    Also patches the OTel span name from logfire's default
    ``Agent run: {name!r}`` to the OTel convention
    ``invoke_agent {agent_name}`` so the Logfire Agents dashboard can
    discover the agent.

    TODO: remove once logfire natively emits these on AgentSpanData spans.
    """
    try:
        import logfire._internal.integrations.openai_agents as _oai_mod
        from agents import AgentSpanData, FunctionSpanData, GenerationSpanData

        # Patch 1: Add OTel GenAI semconv attributes that logfire doesn't set.
        # - AgentSpanData: gen_ai.agent.name, gen_ai.operation.name, etc.
        # - GenerationSpanData: gen_ai.operation.name = "chat"
        # - FunctionSpanData: gen_ai.operation.name = "execute_tool"
        _original = _oai_mod.attributes_from_span_data

        def _patched(span_data: Any, msg_template: str) -> dict[str, Any]:
            attrs = _original(span_data, msg_template)
            if isinstance(span_data, AgentSpanData) and "name" in attrs:
                attrs["gen_ai.agent.name"] = attrs["name"]
                attrs["gen_ai.operation.name"] = "invoke_agent"
                attrs["gen_ai.provider.name"] = "openai"
                model = _copilot_model_name.get()
                if model:
                    attrs["gen_ai.request.model"] = model
            elif isinstance(span_data, GenerationSpanData):
                attrs.setdefault("gen_ai.operation.name", "chat")
            elif isinstance(span_data, FunctionSpanData):
                attrs.setdefault("gen_ai.operation.name", "execute_tool")
                if "name" in attrs:
                    attrs.setdefault("gen_ai.tool.name", attrs["name"])
                # Redact sensitive values in the tool-call arguments before
                # the span is emitted. The `input` attribute is a JSON string
                # serialized by the agents SDK; parse it, apply the shared
                # exact-match redactor, and re-serialize. Only FunctionSpanData
                # is covered here; GenerationSpanData and session history still
                # carry raw values (see follow-up in the copilot eval plan).
                raw_input = attrs.get("input")
                if isinstance(raw_input, str):
                    try:
                        parsed = json.loads(raw_input)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        parsed = None
                    if parsed is not None:
                        try:
                            attrs["input"] = json.dumps(redact_sensitive_fields(parsed))
                        except (TypeError, ValueError) as exc:
                            # Fail closed: if redaction or re-serialization raises, drop
                            # the raw value rather than emitting unredacted input to the
                            # trace backend.
                            attrs["input"] = "[redacted: serialization error]"
                            LOG.warning("Copilot tool-call input redaction failed", error=repr(exc))
            return attrs

        _oai_mod.attributes_from_span_data = _patched

        # Patch 2: Override the OTel span name for AgentSpanData to match the
        # OTel GenAI semantic convention pattern ``invoke_agent {agent_name}``.
        # Logfire uses ``Agent run: {name!r}`` as both the msg_template and the
        # OTel span name; the Agents dashboard looks for the convention name.
        _wrapper_cls = _oai_mod.LogfireTraceProviderWrapper
        _original_create = _wrapper_cls.create_span

        def _patched_create(
            self: Any,
            span_data: Any,
            span_id: str | None = None,
            parent: Any | None = None,
            disabled: bool = False,
        ) -> Any:
            result = _original_create(self, span_data, span_id, parent, disabled)
            if isinstance(span_data, AgentSpanData) and getattr(span_data, "name", None):
                try:
                    logfire_span = result.span_helper.span
                    logfire_span._span_name = f"invoke_agent {span_data.name}"
                except AttributeError as exc:
                    _warn_span_rename_once(exc)
            return result

        _wrapper_cls.create_span = _patched_create
    except (ImportError, AttributeError) as exc:
        # ImportError: logfire's private integration module moved or agents
        # dropped a SpanData subtype. AttributeError: a symbol we reach into
        # was renamed. Either way, degrade gracefully -- the tracing substrate
        # still works, just without the Agents-dashboard semconv attributes.
        LOG.warning(
            "Failed to patch agent span attributes for Logfire Agents dashboard",
            error=repr(exc),
            logfire_private_module=_LOGFIRE_PRIVATE_MODULE,
            expected_symbols=_LOGFIRE_PATCH_SYMBOLS,
        )


def _warn_span_rename_once(exc: BaseException) -> None:
    """Log the span-rename failure at most once per process.

    The failure mode for a logfire private-API rename is systematic, so logging
    per-span would flood the agent runtime with identical warnings.

    ``threading.Event`` provides a race-free once-only gate: ``is_set()`` is a
    cheap read on the hot path, and ``set()`` is internally synchronized.
    """
    if _SPAN_RENAME_WARNED.is_set():
        return
    # A rare double-set under contention is cheaper than serializing every
    # span-rename hit through a lock; Event.set() is idempotent.
    _SPAN_RENAME_WARNED.set()
    LOG.warning(
        "Failed to rename agent span for Logfire Agents dashboard",
        error=repr(exc),
        logfire_private_attribute="span_helper.span._span_name",
    )


def copilot_span(name: str, data: dict[str, Any] | None = None) -> Any:
    """Return a tracing span context manager, or nullcontext() when tracing is off.

    When tracing is on, callers must be inside an active ``agents.tracing.trace()``
    scope. ``custom_span`` outside of a live trace returns a NoOpSpan and emits an
    error log from the openai-agents package.
    """
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
