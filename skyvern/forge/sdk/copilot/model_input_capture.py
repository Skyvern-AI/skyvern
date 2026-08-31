"""Canonical tool-surface evidence for local Copilot model-input captures."""

from __future__ import annotations

import contextvars
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

TOOL_SURFACE_VERSION = "copilot-model-tool-surface-v1"
LOG = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class SerializedToolSurface:
    payload: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class _PendingModelInputCapture:
    path: Path
    payload: dict[str, Any]
    redaction_parameters: dict[str, Any]


_PENDING_MODEL_INPUT_CAPTURE: contextvars.ContextVar[_PendingModelInputCapture | None] = contextvars.ContextVar(
    "_PENDING_MODEL_INPUT_CAPTURE",
    default=None,
)


def serialize_tool_surface(tools: list[Any]) -> SerializedToolSurface:
    """Serialize the ordered FunctionTool contract passed to the model."""

    payload = {
        "version": TOOL_SURFACE_VERSION,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description or "",
                "params_json_schema": tool.params_json_schema,
                "strict_json_schema": tool.strict_json_schema,
            }
            for tool in tools
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return SerializedToolSurface(payload=payload, sha256=hashlib.sha256(encoded).hexdigest())


def register_pending_model_input_capture(
    *,
    path: Path,
    payload: dict[str, Any],
    redaction_parameters: dict[str, Any],
) -> None:
    """Bind an input dump to the next model request in this async context."""

    _PENDING_MODEL_INPUT_CAPTURE.set(
        _PendingModelInputCapture(
            path=path,
            payload=payload,
            redaction_parameters=redaction_parameters,
        )
    )


def clear_pending_model_input_capture() -> None:
    _PENDING_MODEL_INPUT_CAPTURE.set(None)


def attach_tool_surface_to_pending_capture(tools: list[Any]) -> None:
    """Finish a pending dump with the exact surface about to reach the model."""

    pending = _PENDING_MODEL_INPUT_CAPTURE.get()
    _PENDING_MODEL_INPUT_CAPTURE.set(None)
    if pending is None:
        return

    try:
        from skyvern.forge import app

        surface = serialize_tool_surface(tools)
        payload = {
            **pending.payload,
            "tool_surface": surface.payload,
            "tool_surface_sha256": surface.sha256,
        }
        parameters = pending.redaction_parameters
        if parameters:
            payload = app.AGENT_FUNCTION.redact_codeblock_parameter_values(payload, parameters)
        if not isinstance(payload, dict):
            payload = {}
        serialized = json.dumps(payload, indent=2, default=str)
        if parameters:
            serialized = app.AGENT_FUNCTION.redact_codeblock_parameter_values(serialized, parameters)
        pending.path.write_text(serialized if isinstance(serialized, str) else "")
    except Exception:  # noqa: BLE001 - optional capture evidence must never block the model call
        LOG.warning("Failed to attach tool surface to Copilot model-input capture")
