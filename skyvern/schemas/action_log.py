from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

# Deliberate private import: the action log must scrub URLs exactly like synthesis does,
# without forking the scrubber or modifying the copilot module.
from skyvern.forge.sdk.copilot.code_block_synthesis import _scrub_url_for_code_literal
from skyvern.forge.sdk.copilot.typed_value_policy import safe_typed_default_value, typed_text_looks_secret

ACTION_LOG_SCHEMA_VERSION: Literal[1] = 1
ACTION_LOG_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "skyvern_act",
        "skyvern_click",
        "skyvern_clipboard_read",
        "skyvern_clipboard_write",
        "skyvern_drag",
        "skyvern_evaluate",
        "skyvern_execute",
        "skyvern_extract",
        "skyvern_file_upload",
        "skyvern_find",
        "skyvern_frame_list",
        "skyvern_frame_main",
        "skyvern_frame_switch",
        "skyvern_hover",
        "skyvern_login",
        "skyvern_navigate",
        "skyvern_observe",
        "skyvern_press_key",
        "skyvern_run_task",
        "skyvern_screenshot",
        "skyvern_scroll",
        "skyvern_select_option",
        "skyvern_type",
        "skyvern_validate",
        "skyvern_wait",
    }
)
ACTION_LOG_MAX_EVENTS_PER_BATCH = 50
ACTION_LOG_MAX_BODY_BYTES = 256 * 1024
ACTION_LOG_MAX_TOOL_LENGTH = 64
ACTION_LOG_MAX_SELECTOR_LENGTH = 2_048
ACTION_LOG_MAX_VALUE_LENGTH = 2_048
ACTION_LOG_MAX_SOURCE_URL_LENGTH = 4_096
ACTION_LOG_MAX_ERROR_CODE_LENGTH = 128
ACTION_LOG_MAX_ARTIFACT_REF_LENGTH = 512
ACTION_LOG_MAX_TIMING_ENTRIES = 32
ACTION_LOG_MAX_TIMING_KEY_LENGTH = 64
ACTION_LOG_DEFAULT_PAGE_SIZE = 50
ACTION_LOG_MAX_PAGE_SIZE = 100
_REDACTED = "__redacted__"
_QUOTED_SELECTOR_COMPONENT_RE = re.compile(r'(?P<quote>["\'])(?P<value>.*?)(?P=quote)')
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_TIMING_KEY_RE = re.compile(r"[a-z0-9_.:-]+")


def _is_valid_timing_key(key: str) -> bool:
    return (
        bool(key)
        and len(key) <= ACTION_LOG_MAX_TIMING_KEY_LENGTH
        and _TIMING_KEY_RE.fullmatch(key) is not None
        and not typed_text_looks_secret(key)
    )


class ActionLogOutcome(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class ActionLogEvent(BaseModel):
    """Versioned action payload ordered deterministically by ``(occurred_at, event_id)``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = ACTION_LOG_SCHEMA_VERSION
    event_id: UUID
    tool: str = Field(min_length=1, max_length=ACTION_LOG_MAX_TOOL_LENGTH)
    selector: str | None = Field(default=None, max_length=ACTION_LOG_MAX_SELECTOR_LENGTH)
    value: str | None = Field(default=None, max_length=ACTION_LOG_MAX_VALUE_LENGTH)
    source_url: str | None = Field(default=None, max_length=ACTION_LOG_MAX_SOURCE_URL_LENGTH)
    occurred_at: datetime
    timing_ms: dict[str, int] = Field(default_factory=dict)
    outcome: ActionLogOutcome
    error_code: str | None = Field(default=None, max_length=ACTION_LOG_MAX_ERROR_CODE_LENGTH)
    index: int = Field(ge=0)
    artifact_ref: str | None = Field(default=None, max_length=ACTION_LOG_MAX_ARTIFACT_REF_LENGTH)

    @field_validator("tool")
    @classmethod
    def validate_tool(cls, value: str) -> str:
        if value not in ACTION_LOG_ALLOWED_TOOLS:
            raise ValueError("invalid tool")
        return value

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a UTC offset")
        return value.astimezone(timezone.utc)

    @field_validator("timing_ms")
    @classmethod
    def validate_timing_ms(cls, value: dict[str, int]) -> dict[str, int]:
        if len(value) > ACTION_LOG_MAX_TIMING_ENTRIES:
            raise ValueError("too many timing entries")
        if any(not _is_valid_timing_key(key) for key in value):
            raise ValueError("invalid timing key")
        if any(duration < 0 for duration in value.values()):
            raise ValueError("timing durations must be non-negative")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_error_code_shape(cls, value: str | None) -> str | None:
        if value is not None and (_ERROR_CODE_RE.fullmatch(value) is None or typed_text_looks_secret(value)):
            raise ValueError("invalid error code")
        return value

    @model_validator(mode="after")
    def validate_error_code(self) -> ActionLogEvent:
        if self.outcome is ActionLogOutcome.ERROR and not self.error_code:
            raise ValueError("error outcome requires error_code")
        if self.outcome is ActionLogOutcome.SUCCESS and self.error_code is not None:
            raise ValueError("success outcome cannot carry error_code")
        return self

    @field_serializer("occurred_at", when_used="json")
    def serialize_occurred_at(self, value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    @property
    def order_key(self) -> tuple[datetime, int, str]:
        # index breaks same-millisecond ties in true action order; event_id keeps
        # the key deterministic if indexes ever collide across process restarts.
        return self.occurred_at, self.index, str(self.event_id)


class ActionLogBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[ActionLogEvent] = Field(min_length=1, max_length=ACTION_LOG_MAX_EVENTS_PER_BATCH)


class ActionLogBatchResponse(BaseModel):
    accepted: int = Field(ge=0, le=ACTION_LOG_MAX_EVENTS_PER_BATCH)


class ActionLogPage(BaseModel):
    events: list[ActionLogEvent]
    next_cursor: str | None = None


def _scrub_action_log_url(source_url: str) -> str:
    """Apply the replay scrubber, then remove secret-shaped URL components for durable storage."""

    scrubbed = _scrub_url_for_code_literal(source_url)
    scheme, separator, _ = scrubbed.partition(":")
    if separator and typed_text_looks_secret(scheme):
        return _REDACTED
    try:
        parts = urlsplit(scrubbed)
    except ValueError:
        return _REDACTED if typed_text_looks_secret(scrubbed) else scrubbed

    netloc = _REDACTED if typed_text_looks_secret(parts.netloc) else parts.netloc
    path = "/".join(
        _REDACTED if segment and typed_text_looks_secret(segment) else segment for segment in parts.path.split("/")
    )
    query = urlencode(
        [
            (
                _REDACTED if typed_text_looks_secret(key) else key,
                _REDACTED if typed_text_looks_secret(value) else value,
            )
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    fragment_pairs = (
        parse_qsl(parts.fragment, keep_blank_values=True) if "=" in parts.fragment or "&" in parts.fragment else []
    )
    if fragment_pairs:
        fragment = urlencode(
            [
                (
                    _REDACTED if typed_text_looks_secret(key) else key,
                    _REDACTED if typed_text_looks_secret(value) else value,
                )
                for key, value in fragment_pairs
            ],
            doseq=True,
        )
    else:
        fragment = _REDACTED if typed_text_looks_secret(parts.fragment) else parts.fragment
    return urlunsplit((parts.scheme, netloc, path, query, fragment))


def _scrub_action_log_selector(selector: str) -> str:
    def redact_secret_component(match: re.Match[str]) -> str:
        value = match.group("value")
        return (
            f"{match.group('quote')}{_REDACTED}{match.group('quote')}"
            if typed_text_looks_secret(value)
            else match.group(0)
        )

    scrubbed = _QUOTED_SELECTOR_COMPONENT_RE.sub(redact_secret_component, selector)
    return _REDACTED if typed_text_looks_secret(scrubbed) else scrubbed


def _scrub_action_log_string(value: str) -> str:
    return _REDACTED if typed_text_looks_secret(value) else value


def sanitize_action_log_event(event: ActionLogEvent) -> ActionLogEvent:
    return event.model_copy(
        update={
            "selector": _scrub_action_log_selector(event.selector) if event.selector is not None else None,
            "value": _scrub_action_log_string(event.value) if event.value is not None else None,
            "source_url": _scrub_action_log_url(event.source_url) if event.source_url is not None else None,
            "artifact_ref": _scrub_action_log_string(event.artifact_ref) if event.artifact_ref is not None else None,
        }
    )


def project_action_event(
    *,
    event_id: UUID,
    tool: str,
    occurred_at: datetime,
    timing_ms: dict[str, int],
    outcome: ActionLogOutcome,
    index: int,
    selector: str | None = None,
    typed_text: str | None = None,
    value: str | None = None,
    key: str | None = None,
    source_url: str | None = None,
    error_code: str | None = None,
    artifact_ref: str | None = None,
    replay_compatible: bool = False,
) -> ActionLogEvent:
    """Project raw in-scope tool locals into the only payload allowed to leave the CLI process."""

    if typed_text is not None:
        filtered_value = safe_typed_default_value(typed_text, selector=selector or "")
    else:
        candidate = value if value is not None else key
        filtered_value = candidate.strip() if candidate and not typed_text_looks_secret(candidate) else None

    if source_url is None:
        filtered_source_url = None
    elif replay_compatible:
        filtered_source_url = _scrub_url_for_code_literal(source_url)
    else:
        filtered_source_url = _scrub_action_log_url(source_url)

    fields = {
        "schema_version": ACTION_LOG_SCHEMA_VERSION,
        "event_id": event_id,
        "tool": tool,
        "selector": selector,
        "value": filtered_value,
        "source_url": filtered_source_url,
        "occurred_at": occurred_at,
        "timing_ms": (
            dict(timing_ms)
            if replay_compatible
            else {key: duration for key, duration in timing_ms.items() if _is_valid_timing_key(key)}
        ),
        "outcome": outcome,
        "error_code": error_code,
        "index": index,
        "artifact_ref": artifact_ref,
    }
    if replay_compatible:
        # Replay consumes only privacy-projected fields and historically had no action-log field caps.
        return ActionLogEvent.model_construct(**fields)
    return sanitize_action_log_event(ActionLogEvent.model_validate(fields))
