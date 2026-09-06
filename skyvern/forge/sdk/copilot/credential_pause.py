"""Mid-loop pause for copilot BUILD turns blocked on a missing credential.

Parks either inside the ``request_credential`` tool call the model makes, or
between ``Runner.run_streamed`` iterations at the enforcement finalize seam
(see ``run_with_enforcement``) for the run-derived asks, so the SSE connection
stays open while the frontend surfaces a credential-connect card. Resume transport is a
per-turn Redis flag polled directly by the paused coroutine -- the inverse of
the ``/workflow/copilot/cancel`` sidecar watcher, since here the paused loop
itself is the poller rather than a task racing the handler.

The resume path is not authorized by org auth + ``turn_id`` alone. Establishing
the pause writes an *active-pause record*, keyed by (org, chat, turn), that
carries a one-time ``resume_token`` delivered only in the ``credential_required``
frame. ``resolve_credential_pause`` -- the only writer of the loop-facing
response flag -- refuses to store a decision unless the caller presents that
token against a still-pending record, and consumes the record on the first
accepted response so a leaked or replayed ``turn_id`` can't resolve the pause.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.credential_resolution import loggable_origin, url_parts
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import DiagnosisFailureType, RepairNextAction
from skyvern.forge.sdk.copilot.human_input_wait import pause_human_input
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.schemas.credentials import Credential
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotCredentialRequiredUpdate,
    WorkflowCopilotStreamMessageType,
)

if TYPE_CHECKING:
    from agents.result import RunResultStreaming

    # Importing the routes package at module scope pulls in workflow_copilot.py ->
    # agent.py -> enforcement.py, which imports this module -> circular import.
    from skyvern.forge.sdk.core.event_source_stream import EventSourceStream

LOG = structlog.get_logger()

CREDENTIAL_RESPONSE_POLL_SECONDS = 1.5

# The active-pause record and loop-facing response flag outlive the wait window
# by this grace so an accepted response is still readable if the resumed loop is
# briefly slow to poll.
CREDENTIAL_PAUSE_RECORD_TTL_GRACE_SECONDS = 300


def credential_response_cache_key(organization_id: str, chat_id: str, turn_id: str) -> str:
    return f"copilot_credential_response:{organization_id}:{chat_id}:{turn_id}"


def credential_pause_active_key(organization_id: str, chat_id: str, turn_id: str) -> str:
    return f"copilot_credential_pause:{organization_id}:{chat_id}:{turn_id}"


def _credential_pause_lock_key(organization_id: str, chat_id: str, turn_id: str) -> str:
    return f"copilot_credential_pause_lock:{organization_id}:{chat_id}:{turn_id}"


def _credential_pause_record_ttl(timeout_seconds: int) -> timedelta:
    return timedelta(seconds=timeout_seconds + CREDENTIAL_PAUSE_RECORD_TTL_GRACE_SECONDS)


def _new_resume_token() -> str:
    return secrets.token_urlsafe(32)


class CredentialPauseRejection(Exception):
    """Raised by ``resolve_credential_pause`` when a resume attempt is not authorized.

    Carries the HTTP status the route should surface. Kept fastapi-free so this
    module stays importable from the enforcement loop without pulling in routes.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class _ActivePauseRecord:
    resume_token: str
    status: Literal["pending", "consumed"]
    expires_at: datetime


def _encode_active_pause(resume_token: str, expires_at: datetime, *, consumed: bool = False) -> str:
    return json.dumps(
        {
            "resume_token": resume_token,
            "status": "consumed" if consumed else "pending",
            "expires_at": expires_at.isoformat(),
        }
    )


def _decode_active_pause(raw: Any) -> _ActivePauseRecord | None:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("resume_token")
    record_status = data.get("status")
    if not isinstance(token, str) or record_status not in ("pending", "consumed"):
        return None
    try:
        expires_at = datetime.fromisoformat(str(data.get("expires_at")))
    except ValueError:
        return None
    return _ActivePauseRecord(resume_token=token, status=record_status, expires_at=expires_at)


def _validate_pending_pause(record: _ActivePauseRecord | None, resume_token: str) -> _ActivePauseRecord:
    """Shared checks for a resume attempt: a pending, unexpired record bound to this token."""
    if record is None:
        raise CredentialPauseRejection(
            status_code=HTTPStatus.NOT_FOUND,
            detail="No active credential pause for this turn",
        )
    if record.status != "pending":
        raise CredentialPauseRejection(
            status_code=HTTPStatus.CONFLICT,
            detail="Credential pause already resolved",
        )
    # The record's Redis TTL outlives the wait window by design (grace for a
    # slow final poll) -- that's an infra buffer, not permission to accept a
    # response after the waiter gave up and the turn already finalized without
    # it. Enforce the actual deadline the frame told the client about.
    if datetime.now(timezone.utc) >= record.expires_at:
        raise CredentialPauseRejection(
            status_code=HTTPStatus.GONE,
            detail="Credential pause has expired",
        )
    if not resume_token or not secrets.compare_digest(str(resume_token), record.resume_token):
        raise CredentialPauseRejection(
            status_code=HTTPStatus.FORBIDDEN,
            detail="Invalid credential resume token",
        )
    return record


async def check_credential_pause_resumable(
    cache: Any,
    *,
    organization_id: str,
    workflow_copilot_chat_id: str,
    turn_id: str,
    resume_token: str,
) -> None:
    """Read-only precheck: raises the same rejection a resolve would, without consuming anything.

    Lets a caller (the route) validate the token BEFORE doing anything that
    reveals org-scoped information (e.g. a credential-id existence lookup) to
    an unauthenticated-for-this-turn caller who only has org auth.
    """
    active_key = credential_pause_active_key(organization_id, workflow_copilot_chat_id, turn_id)
    record = _decode_active_pause(await cache.get(active_key))
    _validate_pending_pause(record, resume_token)


async def resolve_credential_pause(
    cache: Any,
    *,
    organization_id: str,
    workflow_copilot_chat_id: str,
    turn_id: str,
    resume_token: str,
    action: Literal["connected", "skip"],
    credential_id: str | None,
) -> None:
    """Validate the resume token against the active pause, consume it, then store the decision.

    The security boundary for resuming a paused turn: org auth alone is not
    enough. The caller must present the one-time ``resume_token`` from the
    ``credential_required`` frame, and (org, chat, turn) must name a still-pending
    active-pause record. Under a per-turn lock, the first accepted response flips
    the record to ``consumed`` before writing the loop-facing flag, so a leaked or
    replayed ``turn_id`` can neither resolve a pause it never opened nor overwrite
    a decision already made.
    """
    active_key = credential_pause_active_key(organization_id, workflow_copilot_chat_id, turn_id)
    lock_key = _credential_pause_lock_key(organization_id, workflow_copilot_chat_id, turn_id)
    async with cache.get_lock(lock_key):
        record = _validate_pending_pause(_decode_active_pause(await cache.get(active_key)), resume_token)
        ttl = _credential_pause_record_ttl(settings.WORKFLOW_COPILOT_CREDENTIAL_PAUSE_TIMEOUT_SECONDS)
        await cache.set(active_key, _encode_active_pause(record.resume_token, record.expires_at, consumed=True), ex=ttl)
        await cache.set(
            credential_response_cache_key(organization_id, workflow_copilot_chat_id, turn_id),
            encode_credential_response(action, credential_id),
            ex=ttl,
        )


@dataclass
class CredentialPauseResolution:
    action: Literal["connected", "skip"]
    credential: Credential | None = None


def encode_credential_response(action: Literal["connected", "skip"], credential_id: str | None) -> str:
    return json.dumps({"action": action, "credential_id": credential_id})


def _decode_credential_response(raw: Any) -> tuple[str, str | None] | None:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("action") not in ("connected", "skip"):
        return None
    credential_id = data.get("credential_id")
    return data["action"], credential_id if isinstance(credential_id, str) else None


def credential_pause_reason(ctx: Any) -> str | None:
    """Typed-signal-only detector for a mid-build credential ask.

    Deliberately narrower than ``credential_prompt_reason`` (request_policy.py):
    no text-marker tier, so a REPLY that merely mentions credentials in prose
    can't trigger a pause -- see the SKY-11988 false-positive lesson.
    """
    policy = getattr(ctx, "request_policy", None)
    raw_secret_redacted_draft = (
        isinstance(policy, RequestPolicy)
        and policy.raw_secret_detected
        and policy.raw_secret_handling == "redacted_draft"
    )
    if getattr(ctx, "last_run_skipped_unbound_credentials", False) and not raw_secret_redacted_draft:
        # This is a concrete run result, not a policy or classifier verdict. Preserve it
        # even when legacy context labels the request as skip_test.
        return "workflow_credential_inputs_unbound"

    contract = getattr(ctx, "latest_diagnosis_repair_contract", None)
    if (
        contract is not None
        and contract.repair_decision.next_action == RepairNextAction.ASK
        and contract.diagnosis_result.suspected_failure_type == DiagnosisFailureType.MISSING_CREDENTIAL_OR_INIT
        # MISSING_CREDENTIAL_OR_INIT is a combined category -- diagnosis_repair_contract.py
        # also assigns it to PARAMETER_BINDING_ERROR and org/workflow/browser-session lookup
        # failures that have nothing to do with credentials. Require the specific category so
        # those don't get preempted by a credential card that can't unblock them.
        and "CREDENTIAL_ERROR" in contract.diagnosis_result.root_cause_identity.failure_categories
    ):
        return "missing_credential_run_failure"

    return None


def credential_pause_transport_ready(ctx: Any, copilot_config: CopilotConfig | None) -> bool:
    """Whether a card can be shown at all this turn, independent of what is asking for one.

    Excludes the async-only checks (stream disconnect).
    """
    return (
        copilot_config is not None
        and copilot_config.credential_pause_enabled
        and getattr(ctx, "client_supports_credential_pause", False)
        and not getattr(ctx, "credential_pause_used", False)
        # A same-process-only cache (LocalCache) can't coordinate the poller with a
        # /credential-response POST that may land on a different worker -- gate on a
        # cache that's explicitly known to be shared (Redis) rather than merely non-None.
        and bool(getattr(getattr(app, "CACHE", None), "is_shared", False))
    )


def defang_card_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).replace('"', "").strip()[:200]


def _bind_connected_credential_origin(policy: RequestPolicy, credential: Credential) -> str | None:
    """Stamp the origin this ask was formed against, so a card-created credential with
    no ``tested_url`` still has an intended origin at the fill seam. Written directly rather than
    through ``_record_live_page_admission``, which skips already-resolved ids and would stamp
    nothing here.
    """
    ask_urls = policy.credential_ask_login_page_urls or policy.login_page_urls
    by_origin: dict[str, str] = {}
    for url in ask_urls:
        parts = url_parts(url)
        if parts is None:
            continue
        *_, origin = parts
        by_origin[origin] = url
    if len(by_origin) != 1:
        return None
    admitted_url = next(iter(by_origin.values()))
    policy.live_page_admitted_urls[credential.credential_id] = admitted_url
    return admitted_url


def _apply_connected_credential_to_policy(ctx: Any, policy: RequestPolicy, credential: Credential) -> None:
    """Record ``credential`` as the explicit answer to the product-owned pause.

    The compatibility flags below describe the concrete credential-resume state;
    they are not a generic tool-permission plane. Without them, the resumed
    ``update_and_run_blocks`` call re-skips for the unresolved credential and the
    turn re-asks for the same credential.

    Also un-latches ``test_after_update_done`` so the resumed run records fresh
    verification state after the newly connected credential is applied.
    """
    ctx.test_after_update_done = False
    ctx.credential_pause_connected_credential_id = credential.credential_id
    admitted_url = _bind_connected_credential_origin(policy, credential)
    LOG.info(
        "copilot_credential_pause_connected",
        credential_id=credential.credential_id,
        bound_origin=loggable_origin(admitted_url) if admitted_url else None,
    )
    if credential.credential_id not in {resolved.credential_id for resolved in policy.resolved_credentials}:
        policy.resolved_credentials.append(credential)
    # The card answer is the user naming this credential, and it supersedes any earlier mention:
    # the fill seam's which-credential check is set equality, so adding instead of replacing would
    # refuse the very credential the user just picked.
    policy.current_turn_named_credential_ids = {credential.credential_id}
    policy.allow_run_blocks = True
    policy.clarification_reason = "none"
    policy.allow_missing_credentials_in_draft = False
    policy.requires_user_clarification = False
    # Otherwise credential_prompt_reason() still sees the deferred-draft flag on the
    # terminal RESPONSE and stamps credentialPrompt right next to credentialPause:
    # connected -- a contradictory "still need a credential" signal to the FE.
    policy.credential_draft_deferred_explicitly = False


def _assemble_resume_messages(ctx: Any, text: str) -> list[dict[str, Any]]:
    # Local import avoids a module-load cycle: enforcement.py imports
    # maybe_credential_pause at module scope.
    from skyvern.forge.sdk.copilot.enforcement import _assemble_enforcement_messages, _consume_pending_screenshots

    screenshot_msg = _consume_pending_screenshots(ctx)
    return _assemble_enforcement_messages(screenshot_msg, text)


def _connected_resume_text(credential: Credential) -> str:
    safe_name = defang_card_text(credential.name)
    return (
        f"The user connected saved credential {safe_name} ({credential.credential_id}) via the credential card. "
        "Bind it as the credential parameter and continue the build; run the blocks that were skipped or failed "
        "on the missing credential."
    )


_SKIP_RESUME_TEXT = (
    "The user chose not to connect a credential now. Continue without it: keep the credential parameter "
    "placeholder in the draft, do not ask for the credential again this turn, and finish. If you run a test, "
    "it may stop at the login step."
)


async def _try_resolve_credential_response(
    response_key: str, organization_id: str
) -> CredentialPauseResolution | None | Literal["pending"]:
    cache = app.CACHE
    raw = await cache.get(response_key)
    if not raw:
        return "pending"
    decoded = _decode_credential_response(raw)
    if decoded is None:
        return "pending"
    action, credential_id = decoded
    if action == "skip":
        return CredentialPauseResolution(action="skip")
    if not credential_id:
        return None
    existing = await app.DATABASE.credentials.get_credentials_by_ids([credential_id], organization_id=organization_id)
    if not existing:
        return None
    return CredentialPauseResolution(action="connected", credential=existing[0])


async def _wait_for_credential_response(
    response_key: str,
    ctx: Any,
    stream: EventSourceStream,
    timeout_seconds: int,
) -> CredentialPauseResolution | None:
    # Check once before the sleep loop so a card response posted in the brief
    # window before the first poll doesn't cost a full extra poll interval.
    first = await _try_resolve_credential_response(response_key, ctx.organization_id)
    if first != "pending":
        return first

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(CREDENTIAL_RESPONSE_POLL_SECONDS)
        # Resolve before checking disconnect: an already-posted response must win
        # over a disconnect that happened after the POST (e.g. connect-then-refresh),
        # not get discarded as a timeout.
        resolved = await _try_resolve_credential_response(response_key, ctx.organization_id)
        if resolved != "pending":
            return resolved
        if await stream.is_disconnected():
            return None
    return None


async def _run_credential_pause(
    ctx: Any,
    message: str,
    stream: EventSourceStream,
    copilot_config: CopilotConfig,
    *,
    reason: str,
    login_page_urls: list[str],
) -> CredentialPauseResolution | None:
    """Send the credential card and wait for the user's decision.

    Returns the resolution, or None to let the caller proceed without one
    (kill-switch off, client can't render the frame, already paused once this
    turn, no cache configured or not shared across workers, client gone, or
    the wait timed out).
    """
    if not credential_pause_transport_ready(ctx, copilot_config):
        return None
    # Predicted true (the sync guard chain passed) but bailing below anyway --
    # latch it now so this iteration can't loop back into the same prediction,
    # and tag the outcome "declined" (distinct from "timeout") so the caller
    # knows no frame was ever sent and can fall back to a normal nudge instead
    # of a premature finalize. credential_pause_transport_ready's own docstring
    # notes it excludes the async-only disconnect check, which is the gap here.
    ctx.credential_pause_used = True
    cache = getattr(app, "CACHE", None)
    if cache is None:
        ctx.credential_pause_outcome = "declined"
        return None
    if await stream.is_disconnected():
        ctx.credential_pause_outcome = "declined"
        return None
    policy = getattr(ctx, "request_policy", None)
    # The FE credential card fetches the full org list itself; these ride the frame as `credential_refs`
    # and seed the picker's "Suggested" group (pinned first), so the user still sees the full list.
    credential_refs = list(policy.credential_refs) if isinstance(policy, RequestPolicy) else []
    timeout_seconds = copilot_config.credential_pause_timeout_seconds
    now = datetime.now(timezone.utc)

    organization_id = ctx.organization_id
    chat_id = getattr(ctx, "workflow_copilot_chat_id", None) or ""
    turn_id = getattr(ctx, "turn_id", None) or ""
    resume_token = _new_resume_token()
    expires_at = now + timedelta(seconds=timeout_seconds)
    # Establish the active-pause record before the frame carries the token: the
    # response endpoint refuses to resolve a turn that has no pending record.
    # expires_at is the same deadline the frame tells the client -- the record's
    # own TTL is a separate infra grace, not a resolve-after-timeout allowance.
    await cache.set(
        credential_pause_active_key(organization_id, chat_id, turn_id),
        _encode_active_pause(resume_token, expires_at),
        ex=_credential_pause_record_ttl(timeout_seconds),
    )

    await stream.send(
        WorkflowCopilotCredentialRequiredUpdate(
            type=WorkflowCopilotStreamMessageType.CREDENTIAL_REQUIRED,
            turn_id=turn_id,
            workflow_copilot_chat_id=chat_id,
            resume_token=resume_token,
            reason=reason,
            message=message,
            login_page_urls=login_page_urls,
            credential_refs=credential_refs,
            timeout_seconds=timeout_seconds,
            expires_at=expires_at,
            timestamp=now,
        )
    )

    async def _invalidate_active_pause_record() -> CredentialPauseResolution | None:
        # The waiter can exit (disconnect, genuine timeout, cancellation, or an
        # unexpected error) well before expires_at -- invalidate the record so a
        # late POST (e.g. the tab reconnects) gets a clear 409 instead of silently
        # writing a response nobody will read. Uses the SAME lock
        # resolve_credential_pause takes, with one final response check under it:
        # a POST that validated the record as still-pending an instant before this
        # runs would otherwise get a clean 204 for a response the waiter -- already
        # given up -- will never read. Rescue it if it raced in.
        lock_key = _credential_pause_lock_key(organization_id, chat_id, turn_id)
        async with cache.get_lock(lock_key):
            raced_in = await _try_resolve_credential_response(response_key, organization_id)
            if isinstance(raced_in, CredentialPauseResolution):
                return raced_in
            await cache.set(
                credential_pause_active_key(organization_id, chat_id, turn_id),
                _encode_active_pause(resume_token, expires_at, consumed=True),
                ex=_credential_pause_record_ttl(timeout_seconds),
            )
            return None

    response_key = credential_response_cache_key(organization_id, chat_id, turn_id)

    if await stream.is_disconnected():
        # send() can return True even when the client is already gone (its own
        # protocol contract: "queued for delivery or dropped because the client
        # is gone") -- a disconnect racing the send itself would otherwise wait
        # out the full timeout for a card nobody ever saw. Re-check right after
        # send and treat it the same as the pre-send disconnect guard above.
        await _invalidate_active_pause_record()
        ctx.credential_pause_outcome = "declined"
        return None

    try:
        with pause_human_input(ctx, "credential"):
            resolution = await _wait_for_credential_response(response_key, ctx, stream, timeout_seconds)
    except BaseException:
        # Covers CancelledError (a direct BaseException subclass, not Exception)
        # alongside any unexpected failure in the wait loop itself -- the frame's
        # resume token is already live client-side either way, so the record must
        # not be left pending for the same reason as the disconnect/timeout case.
        # Can't act on a rescued resolution mid-unwind, only avoid corrupting state.
        await _invalidate_active_pause_record()
        raise

    if resolution is None:
        resolution = await _invalidate_active_pause_record()
        if resolution is None:
            ctx.credential_pause_outcome = "timeout"
            return None
    if resolution.action == "skip":
        ctx.credential_pause_outcome = "skipped"
        # A missing_credential_run_failure pause means the diagnosed run left
        # last_test_ok=False; without clearing it, the resumed reply is intercepted
        # by the generic failed-test nudge instead of honoring the skip decision.
        ctx.last_test_ok = None
        return resolution

    credential = resolution.credential
    if credential is None:
        ctx.credential_pause_outcome = "timeout"
        return None
    if isinstance(policy, RequestPolicy):
        _apply_connected_credential_to_policy(ctx, policy, credential)
    ctx.credential_pause_outcome = "connected"
    return resolution


def arm_credential_pause_gate(ctx: Any) -> None:
    """Close the gate as soon as a model response is known to contain a ``request_credential`` call.

    Arming inside the tool coroutine would be too late: the SDK runs one response's tool calls as
    concurrent tasks, so a run tool scheduled first would pass an unarmed gate.
    """
    settled = getattr(ctx, "credential_pause_settled", None)
    if settled is None or settled.is_set():
        ctx.credential_pause_settled = asyncio.Event()


def release_credential_pause_gate(ctx: Any) -> None:
    settled = getattr(ctx, "credential_pause_settled", None)
    if settled is not None:
        settled.set()


async def request_credential_pause(
    ctx: Any,
    *,
    login_page_url: str,
    message: str,
    stream: EventSourceStream,
    copilot_config: CopilotConfig,
) -> CredentialPauseResolution | None:
    """Raise the card from the model's own ``request_credential`` call and wait, inline, for the
    answer, so tool calls the model issued alongside it can await ``credential_pause_settled``."""
    arm_credential_pause_gate(ctx)
    ctx.credential_ask_in_flight = True
    try:
        resolution = await _run_credential_pause(
            ctx,
            message,
            stream,
            copilot_config,
            reason="login_credentials_unresolved",
            login_page_urls=[login_page_url],
        )
        ctx.credential_pause_reaskable_by_run = resolution is None or resolution.action != "connected"
        return resolution
    finally:
        ctx.credential_ask_in_flight = False
        release_credential_pause_gate(ctx)


async def await_pending_credential_pause(ctx: Any) -> None:
    """Wait out a ``request_credential`` ask that is still open, so a run tool the model called in
    parallel with it cannot start a run before the user has answered. Draft edits are not gated:
    they neither run nor sign in."""
    settled = getattr(ctx, "credential_pause_settled", None)
    if settled is None:
        return
    # A call the SDK rejects on its arguments arms the gate without ever reaching the handler that
    # releases it, and the stream cannot exit to run the enforcement backstop while this wait is
    # parked inside it. Bound the wait so that strands one tool call rather than the whole turn.
    try:
        await asyncio.wait_for(settled.wait(), settings.WORKFLOW_COPILOT_CREDENTIAL_PAUSE_TIMEOUT_SECONDS + 30)
    except asyncio.TimeoutError:
        LOG.warning("copilot_credential_gate_wait_timed_out")
        settled.set()


async def maybe_credential_pause(
    ctx: Any,
    result: RunResultStreaming,
    stream: EventSourceStream,
    copilot_config: CopilotConfig,
) -> list[dict[str, Any]] | None:
    """Pause a finalizing turn that hit a typed run-derived credential ask.

    Returns the resume messages to re-enter the loop with, or None to let the
    caller finalize normally.
    """
    reason = credential_pause_reason(ctx)
    if reason is None:
        return None
    # Exactly True, not merely truthy: ctx is Any here, and a partial stand-in returns a truthy
    # object for any attribute, which would hand the budget back on every turn.
    if getattr(ctx, "credential_pause_reaskable_by_run", False) is True:
        # The spent card was a guess the user never answered; this ask has a run behind it. Hand the
        # budget back once, so an unanswered guess cannot silently cost the turn its real card.
        ctx.credential_pause_reaskable_by_run = False
        ctx.credential_pause_used = False
    if not credential_pause_transport_ready(ctx, copilot_config):
        return None
    # Local import: same module-load-cycle reason as _assemble_resume_messages.
    from skyvern.forge.sdk.copilot.enforcement import _parse_normalized_final_response

    parsed = _parse_normalized_final_response(result)
    policy = getattr(ctx, "request_policy", None)
    resolution = await _run_credential_pause(
        ctx,
        str((parsed or {}).get("user_response") or ""),
        stream,
        copilot_config,
        reason=reason,
        login_page_urls=list(policy.login_page_urls) if isinstance(policy, RequestPolicy) else [],
    )
    if resolution is None:
        return None
    if resolution.action == "skip":
        return _assemble_resume_messages(ctx, _SKIP_RESUME_TEXT)
    credential = resolution.credential
    if credential is None:
        return None
    return _assemble_resume_messages(ctx, _connected_resume_text(credential))
