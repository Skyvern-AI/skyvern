"""Recoverable Copilot failure rendering and telemetry helpers."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

import yaml
from pydantic import ValidationError

from skyvern.forge.sdk.api.llm.exceptions import LLMProviderError
from skyvern.forge.sdk.copilot.context import StructuredContext
from skyvern.forge.sdk.copilot.request_policy import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.workflow_credential_utils import URL_CANDIDATE_RE, url_origin
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException

RecoverableFailureKind = Literal["validation", "tool_call", "external_dep", "timeout", "unknown"]

_REASON_SUMMARY_MAX_CHARS = 120
_RECOVERABLE_ERROR_ID_PREFIX = "cpe"
_RECORDED_FAILURE_CREDENTIAL_ID_RE = re.compile(r"\bcred_[A-Za-z0-9][A-Za-z0-9_-]*\b")
_BROWSER_SESSION_ID_RE = re.compile(r"\bpbs_[A-Za-z0-9_-]+\b")
_BROWSER_SESSION_WITH_ID_RE = re.compile(r"\bbrowser session\s+pbs_[A-Za-z0-9_-]+\b", re.IGNORECASE)


@dataclass(frozen=True)
class RecoverableFailure:
    failure_kind: RecoverableFailureKind
    workflow_modified: bool
    reason_summary: str
    internal_error_id: str
    exception_type: str | None = None


def iter_exception_chain(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _new_internal_error_id() -> str:
    return f"{_RECOVERABLE_ERROR_ID_PREFIX}_{uuid.uuid4().hex[:12]}"


def _redact_browser_session_references(value: str) -> str:
    value = _BROWSER_SESSION_WITH_ID_RE.sub("Browser session", value)
    return _BROWSER_SESSION_ID_RE.sub("the browser session", value)


def _is_exception_named(exc: BaseException, name: str) -> bool:
    # Keep this helper import-light for the base/local wheel: importing
    # copilot.enforcement here would pull in the server-only agents package.
    return type(exc).__name__ == name


def clean_recorded_failure_text(value: object, max_chars: int = _REASON_SUMMARY_MAX_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    text = redact_raw_secrets_for_prompt(" ".join(text.split()))
    text = _redact_browser_session_references(text)
    text = _RECORDED_FAILURE_CREDENTIAL_ID_RE.sub("[CREDENTIAL_ID]", text)
    text = URL_CANDIDATE_RE.sub(lambda match: url_origin(match.group(0)) or "[URL]", text)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text


def _reason_summary(value: str) -> str:
    return clean_recorded_failure_text(value, max_chars=_REASON_SUMMARY_MAX_CHARS)


def _failure_kind_for_exception(error: BaseException | None) -> RecoverableFailureKind:
    if error is None:
        return "unknown"
    if any(_is_exception_named(item, "CopilotTotalTimeoutError") for item in iter_exception_chain(error)):
        return "timeout"
    if any(
        _is_exception_named(item, "CopilotUnrecoverableToolError")
        or _is_exception_named(item, "CopilotNonRetriableNavError")
        for item in iter_exception_chain(error)
    ):
        return "tool_call"
    if any(
        isinstance(item, (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException))
        for item in iter_exception_chain(error)
    ):
        return "validation"
    if any(isinstance(item, LLMProviderError) for item in iter_exception_chain(error)):
        return "external_dep"
    return "unknown"


def _reason_for_failure(error: BaseException | None, failure_kind: RecoverableFailureKind) -> str:
    if failure_kind == "timeout":
        return "Copilot timed out before it could finish this turn"
    if failure_kind == "validation":
        return "The workflow update could not be validated"
    if failure_kind == "external_dep":
        return "A Copilot dependency stopped responding"
    if failure_kind == "tool_call":
        chain = list(iter_exception_chain(error)) if error is not None else []
        if any(_is_exception_named(item, "CopilotNonRetriableNavError") for item in chain):
            return "A browser navigation step could not reach the target URL"
        return "A browser or workflow helper stopped responding"
    return "Copilot hit an internal error before it could finish this turn"


def build_recoverable_failure(
    error: BaseException | None,
    *,
    workflow_modified: bool,
    internal_error_id: str | None = None,
) -> RecoverableFailure:
    failure_kind = _failure_kind_for_exception(error)
    reason_summary = _reason_summary(_reason_for_failure(error, failure_kind))
    exception_type = type(error).__name__ if error is not None else None
    return RecoverableFailure(
        failure_kind=failure_kind,
        workflow_modified=workflow_modified,
        reason_summary=reason_summary,
        internal_error_id=internal_error_id or _new_internal_error_id(),
        exception_type=exception_type,
    )


def format_recoverable_failure_reply(failure: RecoverableFailure) -> str:
    workflow_state = "preserved" if failure.workflow_modified else "not modified"
    return (
        f"{failure.reason_summary}. The workflow was {workflow_state}. "
        f"If this persists, reference {failure.internal_error_id} for support."
    )


def merge_failure_into_context(global_llm_context: str | None, failure: RecoverableFailure) -> str:
    structured = StructuredContext.from_json_str(global_llm_context)
    note = f"copilot turn failed: {failure.failure_kind} {failure.internal_error_id}"
    if note not in structured.decisions_made:
        structured.decisions_made.append(note)
    return structured.to_json_str()
