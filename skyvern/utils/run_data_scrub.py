"""The secret scrub applied to a run's data before it is stored.

Tiers: with the run's Mask Secrets on, every secret value of the run; with it off and
``ENABLE_ARTIFACT_VALUE_SCRUB_ALL_RUNS`` on, the run's long secret values. When the run's settings
cannot be read, the data is withheld rather than stored raw. Text artifacts then have credential-shaped strings
replaced under ``ENABLE_ARTIFACT_CREDENTIAL_SHAPE_SCRUB``.
"""

import asyncio
import json
import re
import time
from typing import Any

import structlog
from cachetools import TTLCache

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.artifact.storage.base import FILE_EXTENTSION_MAP
from skyvern.forge.sdk.core import skyvern_context
from skyvern.utils.secret_redaction import (
    MIN_UNANCHORED_SECRET_LENGTH,
    REDACTION_FAILED_STUB,
    redact_credential_shapes_from_bytes,
    redact_har_bytes,
    redact_multi_field_totp_artifact_bytes,
    redact_secrets_from_bytes,
    redact_secrets_from_json_bytes,
)

LOG = structlog.get_logger(__name__)

REDACTABLE_TEXT_ARTIFACT_TYPES: frozenset[ArtifactType] = frozenset(
    {
        ArtifactType.HTML,
        ArtifactType.HTML_SCRAPE,
        ArtifactType.HTML_ACTION,
        ArtifactType.HTML_PRE_SUBMIT,
        ArtifactType.VISIBLE_ELEMENTS_TREE,
        ArtifactType.VISIBLE_ELEMENTS_TREE_TRIMMED,
        ArtifactType.VISIBLE_ELEMENTS_TREE_IN_PROMPT,
        ArtifactType.VISIBLE_ELEMENTS_ID_CSS_MAP,
        ArtifactType.VISIBLE_ELEMENTS_ID_FRAME_MAP,
        ArtifactType.VISIBLE_ELEMENTS_ID_XPATH_MAP,
        ArtifactType.LLM_PROMPT,
        ArtifactType.LLM_REQUEST,
        ArtifactType.LLM_RESPONSE,
        ArtifactType.LLM_RESPONSE_PARSED,
        ArtifactType.LLM_RESPONSE_RENDERED,
        ArtifactType.BROWSER_CONSOLE_LOG,
        ArtifactType.SKYVERN_LOG,
        ArtifactType.SKYVERN_LOG_RAW,
        ArtifactType.HASHED_HREF_MAP,
    }
)


_JSON_TEXT_ARTIFACT_TYPES: frozenset[ArtifactType] = frozenset(
    artifact_type
    for artifact_type in REDACTABLE_TEXT_ARTIFACT_TYPES
    if FILE_EXTENTSION_MAP.get(artifact_type) == "json"
)


# Script files are executed when loaded back, so they get only the multi-field OTP mask, never the full scrub.
# HARs take their own path through `_scrub_har`.
_MULTI_FIELD_TOTP_TEXT_ARTIFACT_TYPES = REDACTABLE_TEXT_ARTIFACT_TYPES | frozenset(
    {
        ArtifactType.EVAL_SCORE,
        ArtifactType.EVAL_TRAJECTORY,
        ArtifactType.EVAL_RUBRICS,
        ArtifactType.SCRIPT_FILE,
    }
)

_WITHHELD_VALUE: str = json.loads(REDACTION_FAILED_STUB)

# An optional UTF-8 byte order mark, any amount of leading whitespace, then the document's first byte.
_JSON_DOCUMENT_START_RE = re.compile(rb"(?:\xef\xbb\xbf)?\s*[\[{]")

_CREDENTIAL_SHAPE_SCRUB_THREAD_BYTES = 32 * 1024


class _WithholdData(Exception):
    pass


_RUNS_WARNED_MISSING_CONTEXT: TTLCache[str, bool] = TTLCache(maxsize=10_000, ttl=3600)


def _warn_if_run_context_missing(artifact_type: ArtifactType | None, workflow_run_id: str | None) -> None:
    # The run's Mask Secrets setting and secret values live only in its in-process context, so a write
    # after the context is gone (or from another process) is stored as if the run had opted out.
    # Warned once per run per process to bound log volume.
    if (
        not settings.ENABLE_SECRET_ARTIFACT_REDACTION
        or workflow_run_id is None
        or workflow_run_id in _RUNS_WARNED_MISSING_CONTEXT
    ):
        return
    try:
        if app.WORKFLOW_CONTEXT_MANAGER.redaction_inputs_for_run(workflow_run_id) is not None:
            return
    except Exception:
        return
    _RUNS_WARNED_MISSING_CONTEXT[workflow_run_id] = True
    LOG.warning(
        "No live run context; storing artifact without the run's secret scrub",
        artifact_type=artifact_type,
        workflow_run_id=workflow_run_id,
    )


def _all_runs_artifact_secret_values(artifact_type: ArtifactType | None, workflow_run_id: str | None) -> set[str]:
    # Shorter values match on word boundaries and would rewrite ordinary words such as a short username;
    # browser diagnostics keep the runtime-code floor they already had.
    values = app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run(
        workflow_run_id, respect_artifact_redaction_flag=False
    )
    long_values = {value for value in values if len(value) >= MIN_UNANCHORED_SECRET_LENGTH}
    if artifact_type in (ArtifactType.HAR, ArtifactType.BROWSER_CONSOLE_LOG):
        long_values |= app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts()
    return long_values


def _resolve_redaction_workflow_run_id(workflow_run_id: str | None) -> str | None:
    if workflow_run_id:
        return workflow_run_id
    context = skyvern_context.current()
    return context.workflow_run_id if context else None


def _placeholder_ids_for_run(workflow_run_id: str | None) -> frozenset[str]:
    # The prompt/response artifacts are the record of what crossed the model boundary, which exempts
    # the run's placeholder ids; scrubbing one here would make the record disagree. Read separately
    # from the secret lookup: losing an exemption only widens the scrub, so it must not take the same
    # bail-out that stops redacting altogether.
    try:
        return app.WORKFLOW_CONTEXT_MANAGER.registered_placeholder_ids_for_run(workflow_run_id)
    except Exception:
        return frozenset()


def _har_redaction_inputs(workflow_run_id: str | None) -> tuple[set[str], frozenset[str]] | None:
    """The secret values and placeholder ids for a HAR scrub, or None when the HAR is stored as is."""
    try:
        resolved_workflow_run_id = _resolve_redaction_workflow_run_id(workflow_run_id)
        if app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled(resolved_workflow_run_id):
            secret_values = app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run(resolved_workflow_run_id)
        else:
            _warn_if_run_context_missing(ArtifactType.HAR, resolved_workflow_run_id)
            # Opted-out runs get the header, cookie and named-field scrub under the global switch, plus
            # runtime-resolved secrets (e.g. verification codes) and, under the all-runs switch, long values.
            if settings.ENABLE_SECRET_ARTIFACT_REDACTION and settings.ENABLE_ARTIFACT_VALUE_SCRUB_ALL_RUNS:
                try:
                    secret_values = _all_runs_artifact_secret_values(ArtifactType.HAR, resolved_workflow_run_id)
                except Exception:
                    LOG.warning("Secret lookup failed; storing HAR with the runtime-code scrub", exc_info=True)
                    secret_values = app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts()
            else:
                secret_values = app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts()
            if (
                not settings.ENABLE_SECRET_ARTIFACT_REDACTION
                and not secret_values
                and not skyvern_context.multi_field_totp_masking_task_ids()
            ):
                return None
    except Exception:
        if not settings.ENABLE_SECRET_ARTIFACT_REDACTION:
            return None
        # The header, cookie and named-field scrub needs no run data, so a failed lookup keeps it.
        LOG.warning("Secret lookup failed; storing HAR with the name-based scrub only", exc_info=True)
        return set(), frozenset()
    return secret_values, _placeholder_ids_for_run(resolved_workflow_run_id)


def _scrub_har(data: bytes, inputs: tuple[set[str], frozenset[str]] | None) -> bytes:
    if skyvern_context.multi_field_totp_masking_task_ids():
        data = redact_har_bytes(data, set())
    if inputs is None:
        return data
    secret_values, placeholder_ids = inputs
    started = time.thread_time()
    try:
        return redact_har_bytes(data, secret_values, multi_field_totp=False, placeholder_ids=placeholder_ids)
    except Exception:
        # Redaction was required and did not complete, so the HAR is withheld rather than stored raw.
        _log_withheld("HAR scrub failed; withholding artifact data", ArtifactType.HAR, data, started)
        return REDACTION_FAILED_STUB


def _log_withheld(message: str, artifact_type: ArtifactType | None, data: bytes, started: float) -> None:
    LOG.warning(
        message,
        artifact_type=artifact_type,
        size_bytes=len(data),
        cpu_seconds=round(time.thread_time() - started, 3),
        exc_info=True,
    )


def credential_shape_scrub_inputs(kind: ArtifactType, workflow_run_id: str | None) -> frozenset[str] | None:
    """The run's placeholder ids to keep, or None when the data is stored without the credential shape scrub."""
    if (
        kind not in REDACTABLE_TEXT_ARTIFACT_TYPES
        or not settings.ENABLE_SECRET_ARTIFACT_REDACTION
        or not settings.ENABLE_ARTIFACT_CREDENTIAL_SHAPE_SCRUB
    ):
        return None
    return _placeholder_ids_for_run(_resolve_redaction_workflow_run_id(workflow_run_id))


def scrub_credential_shape_bytes(kind: ArtifactType, data: bytes, placeholder_ids: frozenset[str] | None) -> bytes:
    if placeholder_ids is None:
        return data
    started = time.thread_time()
    try:
        json_document = kind in _JSON_TEXT_ARTIFACT_TYPES or _JSON_DOCUMENT_START_RE.match(data) is not None
        return redact_credential_shapes_from_bytes(data, json_document=json_document, placeholder_ids=placeholder_ids)
    except Exception:
        _log_withheld("Credential shape scrub failed; withholding artifact data", kind, data, started)
        return REDACTION_FAILED_STUB


def scrub_credential_shapes_of_entries(
    entries: dict[str, bytes], pending: dict[str, tuple[ArtifactType, frozenset[str]]]
) -> dict[str, bytes]:
    return {
        filename: scrub_credential_shape_bytes(kind, entries[filename], placeholder_ids)
        for filename, (kind, placeholder_ids) in pending.items()
    }


def _text_redaction_inputs(
    artifact_type: ArtifactType | None, workflow_run_id: str | None
) -> tuple[set[str], frozenset[str]] | None:
    """The secret values and placeholder ids for a text scrub, or None when the data is stored as is.

    Raises `_WithholdData` when the run's settings could not be read and the data must not be stored raw; the caller
    logs it.
    """
    try:
        resolved_workflow_run_id = _resolve_redaction_workflow_run_id(workflow_run_id)
        if not app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled(resolved_workflow_run_id):
            _warn_if_run_context_missing(artifact_type, resolved_workflow_run_id)
            if settings.ENABLE_SECRET_ARTIFACT_REDACTION and settings.ENABLE_ARTIFACT_VALUE_SCRUB_ALL_RUNS:
                secret_values = _all_runs_artifact_secret_values(artifact_type, resolved_workflow_run_id)
            elif artifact_type == ArtifactType.BROWSER_CONSOLE_LOG:
                # Runtime-resolved secrets (e.g. verification codes) still redact from browser
                # diagnostics under the global switch alone, matching the workflow-finalization floor.
                secret_values = app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts()
            else:
                return None
            if not secret_values and not skyvern_context.multi_field_totp_masking_task_ids():
                return None
        else:
            secret_values = app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run(resolved_workflow_run_id)
    except Exception as error:
        if not settings.ENABLE_SECRET_ARTIFACT_REDACTION:
            return None
        # Without the run's settings it is unknown whether this data holds secrets, so it is withheld.
        raise _WithholdData from error
    placeholder_ids = _placeholder_ids_for_run(resolved_workflow_run_id)
    if not secret_values and not skyvern_context.multi_field_totp_masking_task_ids():
        return None
    return secret_values, placeholder_ids


def _redact_text_bytes(
    data: bytes, artifact_type: ArtifactType | None, inputs: tuple[set[str], frozenset[str]]
) -> bytes:
    secret_values, placeholder_ids = inputs
    # A JSON artifact's root can be a bare number or string (a scalar extraction result).
    if secret_values and (
        artifact_type in _JSON_TEXT_ARTIFACT_TYPES or _JSON_DOCUMENT_START_RE.match(data) is not None
    ):
        scrubbed = redact_secrets_from_json_bytes(data, secret_values, placeholder_ids=placeholder_ids)
        if scrubbed is not None:
            return scrubbed
    return redact_secrets_from_bytes(data, secret_values, multi_field_totp=False, placeholder_ids=placeholder_ids)


def scrub_run_bytes_sync(
    kind: ArtifactType,
    data: bytes,
    workflow_run_id: str | None = None,
    *,
    scrub_credential_shapes: bool = True,
) -> bytes:
    """Async callers use `scrub_run_bytes`, which moves the HAR and credential shape scrubs off the event loop."""
    if kind == ArtifactType.HAR:
        return _scrub_har(data, _har_redaction_inputs(workflow_run_id))
    data = _redact_known_secret_values(kind, data, workflow_run_id)
    if not scrub_credential_shapes:
        return data
    return scrub_credential_shape_bytes(kind, data, credential_shape_scrub_inputs(kind, workflow_run_id))


def _redact_known_secret_values(kind: ArtifactType, data: bytes, workflow_run_id: str | None) -> bytes:
    if kind in _MULTI_FIELD_TOTP_TEXT_ARTIFACT_TYPES:
        data = redact_multi_field_totp_artifact_bytes(data)
    if kind not in REDACTABLE_TEXT_ARTIFACT_TYPES:
        return data
    started = time.thread_time()
    try:
        inputs = _text_redaction_inputs(kind, workflow_run_id)
    except _WithholdData:
        _log_withheld("Secret lookup failed; withholding artifact data", kind, data, started)
        return REDACTION_FAILED_STUB
    if inputs is None:
        return data
    return _redact_text_bytes(data, kind, inputs)


async def scrub_run_bytes(kind: ArtifactType, data: bytes, workflow_run_id: str | None = None) -> bytes:
    # A HAR can be tens of MB and its scrub parses and re-serializes the whole JSON. The run's secret
    # sets are read on the event loop, which owns them; only the scrub runs in the thread.
    if kind == ArtifactType.HAR:
        return await asyncio.to_thread(_scrub_har, data, _har_redaction_inputs(workflow_run_id))
    data = _redact_known_secret_values(kind, data, workflow_run_id)
    placeholder_ids = credential_shape_scrub_inputs(kind, workflow_run_id)
    # The scan holds the GIL inside each regex call, so the thread only lets the loop run between calls. Crafted input
    # under this size was measured at 2.2 to 2.9 s of CPU per MB worst case, so it holds the loop under 0.1 s.
    if placeholder_ids is not None and len(data) >= _CREDENTIAL_SHAPE_SCRUB_THREAD_BYTES:
        return await asyncio.to_thread(scrub_credential_shape_bytes, kind, data, placeholder_ids)
    return scrub_credential_shape_bytes(kind, data, placeholder_ids)


def scrub_run_value(obj: Any, workflow_run_id: str | None = None) -> Any:
    """Scrub a str, or a JSON-serializable dict or list, with the text-artifact tiers; other values pass through.

    A value whose run settings cannot be read becomes a fixed withheld string.
    """
    if not isinstance(obj, (str, dict, list)):
        return obj
    data = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
    data = redact_multi_field_totp_artifact_bytes(data)
    started = time.thread_time()
    try:
        inputs = _text_redaction_inputs(None, workflow_run_id)
    except _WithholdData:
        _log_withheld("Secret lookup failed; withholding artifact data", None, data, started)
        return _WITHHELD_VALUE
    if inputs is not None:
        data = _redact_text_bytes(data, None, inputs)
    return data.decode() if isinstance(obj, str) else json.loads(data)
