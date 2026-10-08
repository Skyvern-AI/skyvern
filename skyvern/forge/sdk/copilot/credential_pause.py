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
carries a one-time ``resume_token`` delivered in the ``credential_required``
frame and authenticated chat history for clients advertising card recovery. ``resolve_credential_pause`` -- the only writer of the loop-facing
response flag -- refuses to store a decision unless the caller presents that
token against a still-pending record, and consumes the record on the first
accepted response so a leaked or replayed ``turn_id`` can't resolve the pause.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlparse

import structlog
from pydantic import ValidationError

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.browser_action_policy import canonicalize_origin
from skyvern.forge.sdk.cache.base import BaseCache
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.credential_resolution import loggable_origin, safe_admitted_url, url_parts
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import DiagnosisFailureType, RepairNextAction
from skyvern.forge.sdk.copilot.human_input_wait import pause_human_input
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.schemas.credentials import Credential
from skyvern.forge.sdk.schemas.workflow_copilot import (
    CredentialPauseResolvedOutcome,
    WorkflowCopilotCredentialPauseResolvedUpdate,
    WorkflowCopilotCredentialRegistration,
    WorkflowCopilotCredentialRequiredUpdate,
    WorkflowCopilotStreamMessageType,
)

if TYPE_CHECKING:
    from agents.result import RunResultStreaming

    from skyvern.forge.sdk.copilot.context import CopilotContext

    # Importing the routes package at module scope pulls in workflow_copilot.py ->
    # agent.py -> enforcement.py, which imports this module -> circular import.
    from skyvern.forge.sdk.core.event_source_stream import EventSourceStream

LOG = structlog.get_logger()

# Bounds how long after the /credential-response write the stream tab sees the answered card.
CREDENTIAL_RESPONSE_POLL_SECONDS = 0.25

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


# A Done claim reserves the pause while the profile is saved outside the lock; the save itself is bounded
# tighter so the claim always outlives it. A Generate and save claim reuses the same window.
MANUAL_SIGN_IN_CLAIM_SECONDS = 120
MANUAL_SIGN_IN_SAVE_TIMEOUT_SECONDS = 90


def _credential_pause_record_ttl(timeout_seconds: int) -> timedelta:
    return timedelta(seconds=timeout_seconds + CREDENTIAL_PAUSE_RECORD_TTL_GRACE_SECONDS)


def longest_credential_pause_seconds() -> int:
    """A sign-in can start as the card's wait expires and then hold a Done claim, so all three add up."""
    return (
        settings.WORKFLOW_COPILOT_CREDENTIAL_PAUSE_TIMEOUT_SECONDS
        + settings.WORKFLOW_COPILOT_MANUAL_SIGN_IN_TIMEOUT_SECONDS
        + MANUAL_SIGN_IN_CLAIM_SECONDS
    )


def _longest_credential_pause_record_ttl() -> timedelta:
    return _credential_pause_record_ttl(longest_credential_pause_seconds())


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
class ManualSignIn:
    """What a card that offers signing in yourself needs on Done, recorded server-side when the card is raised."""

    browser_session_id: str
    login_urls: list[str]
    profile_name: str
    started: bool = False


@dataclass
class _ActivePauseRecord:
    resume_token: str
    status: Literal["pending", "resolving", "consumed"]
    expires_at: datetime
    card: WorkflowCopilotCredentialRequiredUpdate | None = None
    recovery_token_digest: str | None = None
    manual_sign_in: ManualSignIn | None = None
    claim_id: str | None = None
    claim_deadline: datetime | None = None
    registration: WorkflowCopilotCredentialRegistration | None = None
    generated_credential_id: str | None = None

    def live_until(self) -> datetime | None:
        if self.status == "pending":
            return self.expires_at
        if self.status == "resolving":
            return self.claim_deadline
        return None


def _encode_record(record: _ActivePauseRecord) -> str:
    sign_in = record.manual_sign_in
    return json.dumps(
        {
            "resume_token": record.resume_token,
            "status": record.status,
            "expires_at": record.expires_at.isoformat(),
            "card": record.card.model_dump(mode="json") if record.card is not None else None,
            "recovery_token_digest": record.recovery_token_digest,
            "manual_sign_in": (
                {
                    "browser_session_id": sign_in.browser_session_id,
                    "login_urls": sign_in.login_urls,
                    "profile_name": sign_in.profile_name,
                    "started": sign_in.started,
                }
                if sign_in is not None
                else None
            ),
            "claim_id": record.claim_id,
            "claim_deadline": record.claim_deadline.isoformat() if record.claim_deadline else None,
            "registration": record.registration.model_dump(mode="json") if record.registration else None,
            "generated_credential_id": record.generated_credential_id,
        }
    )


def _encode_active_pause(
    resume_token: str,
    expires_at: datetime,
    *,
    consumed: bool = False,
    card: WorkflowCopilotCredentialRequiredUpdate | None = None,
    recovery_token_digest: str | None = None,
    manual_sign_in: ManualSignIn | None = None,
    registration: WorkflowCopilotCredentialRegistration | None = None,
) -> str:
    return _encode_record(
        _ActivePauseRecord(
            resume_token=resume_token,
            status="consumed" if consumed else "pending",
            expires_at=expires_at,
            card=card,
            recovery_token_digest=recovery_token_digest,
            manual_sign_in=manual_sign_in,
            registration=registration,
        )
    )


def _decode_manual_sign_in(data: Any) -> ManualSignIn | None:
    if not isinstance(data, dict):
        return None
    session_id = data.get("browser_session_id")
    login_urls = data.get("login_urls")
    profile_name = data.get("profile_name")
    if (
        not isinstance(session_id, str)
        or not isinstance(profile_name, str)
        or not isinstance(login_urls, list)
        or not all(isinstance(url, str) for url in login_urls)
    ):
        return None
    return ManualSignIn(
        browser_session_id=session_id,
        login_urls=login_urls,
        profile_name=profile_name,
        started=data.get("started") is True,
    )


def _decode_datetime(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def _decode_active_pause(raw: Any) -> _ActivePauseRecord | None:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("resume_token")
    record_status = data.get("status")
    if not isinstance(token, str) or record_status not in ("pending", "resolving", "consumed"):
        return None
    expires_at = _decode_datetime(data.get("expires_at"))
    if expires_at is None:
        return None
    try:
        card = WorkflowCopilotCredentialRequiredUpdate.model_validate(data["card"]) if data.get("card") else None
    except ValidationError:
        card = None
    try:
        registration = (
            WorkflowCopilotCredentialRegistration.model_validate(data["registration"])
            if data.get("registration")
            else None
        )
    except ValidationError:
        registration = None
    digest = data.get("recovery_token_digest")
    claim_id = data.get("claim_id")
    generated_credential_id = data.get("generated_credential_id")
    return _ActivePauseRecord(
        resume_token=token,
        status=record_status,
        expires_at=expires_at,
        card=card,
        recovery_token_digest=digest if isinstance(digest, str) else None,
        manual_sign_in=_decode_manual_sign_in(data.get("manual_sign_in")),
        claim_id=claim_id if isinstance(claim_id, str) else None,
        claim_deadline=_decode_datetime(data.get("claim_deadline")),
        registration=registration,
        generated_credential_id=generated_credential_id if isinstance(generated_credential_id, str) else None,
    )


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


def credential_recovery_token_digest(token: str | None) -> str | None:
    """Hash a 32-byte browser capability without retaining the bearer value."""
    if not isinstance(token, str) or len(token) != 64:
        return None
    try:
        decoded = bytes.fromhex(token)
    except ValueError:
        return None
    if len(decoded) != 32:
        return None
    return hashlib.sha256(token.encode()).hexdigest()


class _CredentialRecoveryContext(Protocol):
    credential_recovery_armed: bool
    client_supports_credential_pause_recovery: bool
    credential_recovery_token_digest: str | None


def _credential_recovery_enabled(ctx: _CredentialRecoveryContext) -> bool:
    return bool(
        ctx.credential_recovery_armed
        and ctx.client_supports_credential_pause_recovery
        and ctx.credential_recovery_token_digest
    )


async def pending_credential_requests(
    organization_id: str,
    chat_id: str,
    turn_ids: list[str],
    recovery_token: str | None,
) -> list[WorkflowCopilotCredentialRequiredUpdate]:
    digest = credential_recovery_token_digest(recovery_token)
    cache = app.CACHE
    if cache is None or digest is None:
        return []
    cards: list[WorkflowCopilotCredentialRequiredUpdate] = []
    for turn_id in turn_ids:
        try:
            raw = await cache.get(credential_pause_active_key(organization_id, chat_id, turn_id))
        except Exception:
            LOG.warning("Failed to recover Copilot credential pause", exc_info=True)
            raise
        record = _decode_active_pause(raw)
        if (
            record is None
            or record.card is None
            or record.recovery_token_digest is None
            or not secrets.compare_digest(digest, record.recovery_token_digest)
        ):
            continue
        live_until = record.live_until()
        # A Done being saved still owns the card: if it finds no sign-in, the pause reopens for this tab.
        saving = record.status == "resolving" and live_until is not None and datetime.now(timezone.utc) < live_until
        if not saving:
            try:
                _validate_pending_pause(record, record.resume_token)
            except CredentialPauseRejection:
                continue
        card = record.card
        if (
            card.turn_id == turn_id
            and card.workflow_copilot_chat_id == chat_id
            and card.resume_token == record.resume_token
            and card.expires_at == record.expires_at
        ):
            registration = record.registration
            cards.append(card.model_copy(update={"registration": registration}) if registration else card)
    return cards


async def credential_pause_is_active(organization_id: str, chat_id: str, turn_id: str) -> bool | None:
    """Report whether a live waiter owns the turn, or ``None`` when Redis is unavailable."""
    cache = app.CACHE
    if cache is None:
        return False
    try:
        record = _decode_active_pause(await cache.get(credential_pause_active_key(organization_id, chat_id, turn_id)))
    except Exception:
        LOG.warning("Failed to inspect Copilot credential pause", exc_info=True)
        return None
    live_until = record.live_until() if record is not None else None
    return bool(live_until is not None and datetime.now(timezone.utc) < live_until)


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


async def _record_turn_resumed(organization_id: str, chat_id: str, turn_id: str) -> None:
    # Reconcile reads the pause before the turn marker, so stamping first leaves no instant where a
    # resumed turn shows neither a live pause nor a resume and can be reclaimed as abandoned.
    try:
        await app.DATABASE.workflow_params.record_pending_copilot_turn_credential_resume(
            organization_id=organization_id,
            workflow_copilot_chat_id=chat_id,
            turn_id=turn_id,
        )
    except Exception:
        LOG.warning("Could not record the credential pause resume on the turn marker", exc_info=True)


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
    await _record_turn_resumed(organization_id, workflow_copilot_chat_id, turn_id)
    async with cache.get_lock(lock_key):
        record = _validate_pending_pause(_decode_active_pause(await cache.get(active_key)), resume_token)
        ttl = _credential_pause_record_ttl(settings.WORKFLOW_COPILOT_CREDENTIAL_PAUSE_TIMEOUT_SECONDS)
        await cache.set(
            active_key,
            _encode_active_pause(
                record.resume_token, record.expires_at, consumed=True, registration=record.registration
            ),
            ex=ttl,
        )
        await cache.set(
            credential_response_cache_key(organization_id, workflow_copilot_chat_id, turn_id),
            encode_credential_response(action, credential_id, record.resume_token),
            ex=ttl,
        )


@dataclass(frozen=True)
class SignedInProfile:
    browser_profile_id: str
    profile_name: str
    site: str
    cookie_count: int


@dataclass
class CredentialPauseResolution:
    action: Literal["connected", "skip", "signed_in"]
    credential: Credential | None = None
    signed_in: SignedInProfile | None = None
    generated: bool = False


def encode_credential_response(
    action: Literal["connected", "skip", "signed_in"],
    credential_id: str | None,
    resume_token: str,
    signed_in: SignedInProfile | None = None,
) -> str:
    payload: dict[str, Any] = {"action": action, "credential_id": credential_id, "resume_token": resume_token}
    if signed_in is not None:
        payload["signed_in"] = {
            "browser_profile_id": signed_in.browser_profile_id,
            "profile_name": signed_in.profile_name,
            "site": signed_in.site,
            "cookie_count": signed_in.cookie_count,
        }
    return json.dumps(payload)


def _decode_signed_in(data: Any) -> SignedInProfile | None:
    if not isinstance(data, dict):
        return None
    profile_id, name, site, count = (
        data.get("browser_profile_id"),
        data.get("profile_name"),
        data.get("site"),
        data.get("cookie_count"),
    )
    if not (isinstance(profile_id, str) and isinstance(name, str) and isinstance(site, str) and isinstance(count, int)):
        return None
    return SignedInProfile(browser_profile_id=profile_id, profile_name=name, site=site, cookie_count=count)


def _decode_credential_response(raw: Any) -> tuple[str, str | None, str | None, SignedInProfile | None] | None:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("action") not in ("connected", "skip", "signed_in"):
        return None
    credential_id = data.get("credential_id")
    resume_token = data.get("resume_token")
    return (
        data["action"],
        credential_id if isinstance(credential_id, str) else None,
        resume_token if isinstance(resume_token, str) else None,
        _decode_signed_in(data.get("signed_in")),
    )


def sign_in_site(login_urls: list[str]) -> str:
    return next((host for url in login_urls if (host := (urlparse(url).hostname or "").lower())), "")


def manual_sign_in_profile_name(site: str, workflow_title: str | None) -> str:
    title = defang_card_text(workflow_title or "")
    return f"Sign-in for {site} ({title})" if title else f"Sign-in for {site}"


async def start_manual_sign_in(
    cache: BaseCache,
    *,
    organization_id: str,
    workflow_copilot_chat_id: str,
    turn_id: str,
    resume_token: str,
) -> datetime:
    """Restart the pause countdown, once, for a user signing in themselves; returns the new deadline."""
    active_key = credential_pause_active_key(organization_id, workflow_copilot_chat_id, turn_id)
    async with cache.get_lock(_credential_pause_lock_key(organization_id, workflow_copilot_chat_id, turn_id)):
        record = _validate_pending_pause(_decode_active_pause(await cache.get(active_key)), resume_token)
        sign_in = record.manual_sign_in
        if sign_in is None:
            raise CredentialPauseRejection(
                status_code=HTTPStatus.CONFLICT,
                detail="This credential card does not offer signing in yourself",
            )
        if sign_in.started:
            return record.expires_at
        record = _extend_pause(
            replace(record, manual_sign_in=replace(sign_in, started=True)),
            settings.WORKFLOW_COPILOT_MANUAL_SIGN_IN_TIMEOUT_SECONDS,
            signing_in=True,
        )
        await cache.set(active_key, _encode_record(record), ex=_longest_credential_pause_record_ttl())
        return record.expires_at


def _extend_pause(record: _ActivePauseRecord, seconds: int, **card_update: bool) -> _ActivePauseRecord:
    expires_at = max(record.expires_at, datetime.now(timezone.utc) + timedelta(seconds=seconds))
    # The recovered card must carry the record's deadline, or pending_credential_requests drops it.
    card = record.card.model_copy(update={"expires_at": expires_at, **card_update}) if record.card else None
    return replace(record, expires_at=expires_at, card=card)


@dataclass(frozen=True)
class ManualSignInClaim:
    claim_id: str
    resume_token: str
    sign_in: ManualSignIn


async def claim_manual_sign_in(
    cache: BaseCache,
    *,
    organization_id: str,
    workflow_copilot_chat_id: str,
    turn_id: str,
    resume_token: str,
) -> ManualSignInClaim:
    """Reserve a pending sign-in card for one Done while its profile is saved outside the pause lock."""
    active_key = credential_pause_active_key(organization_id, workflow_copilot_chat_id, turn_id)
    async with cache.get_lock(_credential_pause_lock_key(organization_id, workflow_copilot_chat_id, turn_id)):
        record = _validate_pending_pause(_decode_active_pause(await cache.get(active_key)), resume_token)
        if record.manual_sign_in is None:
            raise CredentialPauseRejection(
                status_code=HTTPStatus.CONFLICT,
                detail="This credential card does not offer signing in yourself",
            )
        claim_id = secrets.token_hex(16)
        claimed = replace(
            record,
            status="resolving",
            claim_id=claim_id,
            claim_deadline=datetime.now(timezone.utc) + timedelta(seconds=MANUAL_SIGN_IN_CLAIM_SECONDS),
        )
        await cache.set(active_key, _encode_record(claimed), ex=_longest_credential_pause_record_ttl())
        return ManualSignInClaim(claim_id=claim_id, resume_token=record.resume_token, sign_in=record.manual_sign_in)


async def finish_manual_sign_in(
    cache: BaseCache,
    *,
    organization_id: str,
    workflow_copilot_chat_id: str,
    turn_id: str,
    claim: ManualSignInClaim,
    signed_in: SignedInProfile | None,
) -> bool:
    """Settle a Done claim; False means the claim was lost and the caller must discard the profile."""
    active_key = credential_pause_active_key(organization_id, workflow_copilot_chat_id, turn_id)
    if signed_in is not None:
        await _record_turn_resumed(organization_id, workflow_copilot_chat_id, turn_id)
    async with cache.get_lock(_credential_pause_lock_key(organization_id, workflow_copilot_chat_id, turn_id)):
        record = _decode_active_pause(await cache.get(active_key))
        if record is None or record.status != "resolving" or record.claim_id != claim.claim_id:
            return False
        ttl = _longest_credential_pause_record_ttl()
        if signed_in is None:
            reopened = replace(record, status="pending", claim_id=None, claim_deadline=None)
            await cache.set(active_key, _encode_record(reopened), ex=ttl)
            return True
        await cache.set(active_key, _encode_active_pause(record.resume_token, record.expires_at, consumed=True), ex=ttl)
        await cache.set(
            credential_response_cache_key(organization_id, workflow_copilot_chat_id, turn_id),
            encode_credential_response("signed_in", None, record.resume_token, signed_in=signed_in),
            ex=ttl,
        )
        return True


@dataclass(frozen=True)
class CredentialGenerationClaim:
    claim_id: str
    registration: WorkflowCopilotCredentialRegistration
    deadline: datetime


async def claim_credential_generation(
    cache: BaseCache,
    *,
    organization_id: str,
    workflow_copilot_chat_id: str,
    turn_id: str,
    resume_token: str,
) -> CredentialGenerationClaim:
    """Reserve a registration card's one Generate and save while the credential is created outside the lock."""
    active_key = credential_pause_active_key(organization_id, workflow_copilot_chat_id, turn_id)
    async with cache.get_lock(_credential_pause_lock_key(organization_id, workflow_copilot_chat_id, turn_id)):
        record = _validate_pending_pause(_decode_active_pause(await cache.get(active_key)), resume_token)
        registration = record.registration
        if registration is None:
            raise CredentialPauseRejection(
                status_code=HTTPStatus.CONFLICT,
                detail="This credential card does not offer generating a credential",
            )
        if registration.attempted:
            raise CredentialPauseRejection(
                status_code=HTTPStatus.CONFLICT,
                detail="This credential card already tried to generate a credential",
            )
        claim_id = secrets.token_hex(16)
        registration = registration.model_copy(update={"attempted": True})
        deadline = datetime.now(timezone.utc) + timedelta(seconds=MANUAL_SIGN_IN_CLAIM_SECONDS)
        claimed = replace(
            record, status="resolving", claim_id=claim_id, claim_deadline=deadline, registration=registration
        )
        await cache.set(active_key, _encode_record(claimed), ex=_longest_credential_pause_record_ttl())
        return CredentialGenerationClaim(claim_id=claim_id, registration=registration, deadline=deadline)


async def finish_credential_generation(
    cache: BaseCache,
    *,
    organization_id: str,
    workflow_copilot_chat_id: str,
    turn_id: str,
    claim: CredentialGenerationClaim,
    credential_id: str | None,
    outcome: Literal["rejected", "unknown"] | None = None,
) -> datetime | None:
    """Connect the created credential, or reopen the card without Generate and with a fresh deadline.

    Returns the pause's deadline, or None when the claim was lost."""
    active_key = credential_pause_active_key(organization_id, workflow_copilot_chat_id, turn_id)
    if credential_id is not None:
        await _record_turn_resumed(organization_id, workflow_copilot_chat_id, turn_id)
    async with cache.get_lock(_credential_pause_lock_key(organization_id, workflow_copilot_chat_id, turn_id)):
        record = _decode_active_pause(await cache.get(active_key))
        if record is None or record.status != "resolving" or record.claim_id != claim.claim_id:
            return None
        ttl = _longest_credential_pause_record_ttl()
        if credential_id is None:
            registration = claim.registration.model_copy(update={"outcome": outcome})
            reopened = _extend_pause(
                replace(record, status="pending", claim_id=None, claim_deadline=None, registration=registration),
                settings.WORKFLOW_COPILOT_CREDENTIAL_PAUSE_TIMEOUT_SECONDS,
            )
            await cache.set(active_key, _encode_record(reopened), ex=ttl)
            return reopened.expires_at
        consumed = _ActivePauseRecord(
            record.resume_token, "consumed", record.expires_at, generated_credential_id=credential_id
        )
        await cache.set(active_key, _encode_record(consumed), ex=ttl)
        await cache.set(
            credential_response_cache_key(organization_id, workflow_copilot_chat_id, turn_id),
            encode_credential_response("connected", credential_id, record.resume_token),
            ex=ttl,
        )
        return record.expires_at


def credential_pause_reason(ctx: Any) -> str | None:
    """Typed-signal-only detector for a run-derived credential ask; reply prose never counts."""
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


RAW_SECRET_CONNECTED_NEXT = (
    "Bind this credential as the workflow's credential parameter in the draft. This turn contains a "
    "redacted secret, so do not run, test, or use the browser; the draft stays untested until a later "
    "message asks to test it."
)


def raw_secret_card_origin(user_url: str) -> str:
    # A pasted URL can carry the secret in its path or query, so only its origin reaches the card; like the
    # fill seam's site check, a URL with userinfo has no admissible origin at all.
    # A schemeless user site (www.example.com/login) is admitted elsewhere with https assumed.
    if canonicalize_origin(user_url if "://" in user_url else f"https://{user_url}") is None:
        return ""
    scheme, separator, rest = safe_admitted_url(user_url).partition("://")
    return f"{scheme}{separator}{rest.split('/', 1)[0]}" if separator else ""


def credential_pause_transport_ready(
    ctx: CopilotContext, copilot_config: CopilotConfig | None, *, allow_second_ask: bool = False
) -> bool:
    """Whether a card can be shown at all this turn, independent of what is asking for one.

    Excludes the async-only checks (stream disconnect).
    """
    return (
        copilot_config is not None
        and copilot_config.credential_pause_enabled
        and ctx.client_supports_credential_pause
        and (allow_second_ask or not ctx.credential_pause_used)
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
    """Record ``credential`` as the explicit answer to the product-owned pause, reopening the run gate
    (and un-latching ``test_after_update_done``) so the resumed run does not re-skip and re-ask; a
    raw-secret turn keeps its run gate closed and its draft untested for a later secret-free turn."""
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
    policy.clarification_reason = "none"
    policy.requires_user_clarification = False
    if not policy.raw_secret_detected:
        ctx.test_after_update_done = False
        policy.allow_run_blocks = True
        policy.allow_missing_credentials_in_draft = False
    # Otherwise credential_prompt_reason() still sees the deferred-draft flag on the
    # terminal RESPONSE and stamps credentialPrompt right next to credentialPause:
    # connected -- a contradictory "still need a credential" signal to the FE.
    policy.credential_draft_deferred_explicitly = False


def _apply_signed_in_profile_to_policy(ctx: Any, policy: RequestPolicy, signed_in: SignedInProfile) -> None:
    """Record the user's own sign-in as this turn's answer: its profile may seed this turn's runs, and the
    credential ask is settled, without any credential entering the authority plane."""
    policy.credential_pause_signed_in_profile_id = signed_in.browser_profile_id
    policy.credential_pause_signed_in_site = signed_in.site
    policy.clarification_reason = "none"
    policy.requires_user_clarification = False
    policy.credential_draft_deferred_explicitly = False
    ctx.test_after_update_done = False
    policy.allow_run_blocks = True
    LOG.info(
        "copilot_credential_pause_signed_in",
        browser_profile_id=signed_in.browser_profile_id,
        site=signed_in.site,
        cookie_count=signed_in.cookie_count,
    )


def signed_in_facts(signed_in: SignedInProfile) -> str:
    return (
        f"The user signed in to {signed_in.site} themselves in the live browser via the credential card. "
        f"Skyvern saved that sign-in as browser profile {defang_card_text(signed_in.profile_name)} "
        f"({signed_in.browser_profile_id}) with {signed_in.cookie_count} cookies. No credential was provided."
    )


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
    response_key: str, organization_id: str, resume_token: str
) -> CredentialPauseResolution | None | Literal["pending"]:
    cache = app.CACHE
    raw = await cache.get(response_key)
    if not raw:
        return "pending"
    decoded = _decode_credential_response(raw)
    if decoded is None:
        return "pending"
    action, credential_id, answered_token, signed_in = decoded
    # The response key is per turn, so a card only takes an answer carrying its own token.
    if answered_token != resume_token:
        return "pending"
    if action == "skip":
        return CredentialPauseResolution(action="skip")
    if action == "signed_in":
        return CredentialPauseResolution(action="signed_in", signed_in=signed_in) if signed_in else None
    if not credential_id:
        return None
    existing = await app.DATABASE.credentials.get_credentials_by_ids([credential_id], organization_id=organization_id)
    if not existing:
        return None
    return CredentialPauseResolution(action="connected", credential=existing[0])


async def _wait_for_credential_response(
    response_key: str,
    ctx: CopilotContext,
    stream: EventSourceStream,
    timeout_seconds: float,
    resume_token: str,
) -> CredentialPauseResolution | None | Literal["disconnected"]:
    recovery_enabled = _credential_recovery_enabled(ctx)
    # Check once before the sleep loop so a card response posted in the brief
    # window before the first poll doesn't cost a full extra poll interval.
    first = await _try_resolve_credential_response(response_key, ctx.organization_id, resume_token)
    if first != "pending":
        return first

    deadline = time.monotonic() + timeout_seconds
    while True:
        if time.monotonic() >= deadline:
            # A sign-in the user started, or a Done being saved, moves the record's deadline past ours.
            remaining = await _seconds_left_on_pause(ctx, resume_token)
            if remaining <= 0:
                return None
            deadline = time.monotonic() + remaining
        await asyncio.sleep(CREDENTIAL_RESPONSE_POLL_SECONDS)
        # Resolve before checking disconnect: an already-posted response must win
        # over a disconnect that happened after the POST (e.g. connect-then-refresh),
        # not get discarded as a timeout.
        resolved = await _try_resolve_credential_response(response_key, ctx.organization_id, resume_token)
        if resolved != "pending":
            return resolved
        if not recovery_enabled and await stream.is_disconnected():
            return "disconnected"


async def _seconds_left_on_pause(ctx: CopilotContext, resume_token: str) -> float:
    key = credential_pause_active_key(ctx.organization_id, ctx.workflow_copilot_chat_id or "", ctx.turn_id or "")
    record = _decode_active_pause(await app.CACHE.get(key))
    live_until = record.live_until() if record is not None and record.resume_token == resume_token else None
    return (live_until - datetime.now(timezone.utc)).total_seconds() if live_until is not None else 0.0


def _manual_sign_in_offer(ctx: CopilotContext, login_page_urls: list[str]) -> ManualSignIn | None:
    """A pick card offers signing in yourself in the editor's live browser when there is one to sign in to."""
    policy = ctx.request_policy
    site = sign_in_site(login_page_urls)
    if (
        not ctx.browser_session_id
        or not site
        or ctx.credential_origin_recovery is not None
        or (isinstance(policy, RequestPolicy) and policy.raw_secret_detected)
    ):
        return None
    title = ctx.last_workflow.title if ctx.last_workflow is not None else ctx.opening_workflow_title
    return ManualSignIn(
        browser_session_id=ctx.browser_session_id,
        login_urls=list(login_page_urls),
        profile_name=manual_sign_in_profile_name(site, title),
    )


async def _run_credential_pause(
    ctx: CopilotContext,
    message: str,
    stream: EventSourceStream,
    copilot_config: CopilotConfig,
    *,
    reason: str,
    login_page_urls: list[str],
    update_credential_id: str | None = None,
    admit_connected: Callable[[Credential], Awaitable[bool]] | None = None,
    allow_second_ask: bool = False,
    anchor_tool_call_id: str | None = None,
    registration: WorkflowCopilotCredentialRegistration | None = None,
) -> CredentialPauseResolution | None:
    """Send the credential card and wait for the user's decision.

    Returns the resolution, or None to let the caller proceed without one
    (kill-switch off, client can't render the frame, already paused once this
    turn, no shared cache, unrecoverable client disconnect, or timeout).
    Recovery-capable clients restore the same active card through chat history.
    """
    update_ask = update_credential_id is not None
    ctx.credential_registration_outcome = None
    if not credential_pause_transport_ready(ctx, copilot_config, allow_second_ask=update_ask or allow_second_ask):
        return None
    # Latch before async checks so a declined transport cannot trigger another pause. Only the pick ask
    # spends credential_pause_used; request_credential_pause latches the update ask.
    # A pause no tool call raised (the end-of-turn ask) renders after the newest row instead.
    if anchor_tool_call_id is None and ctx.narrator_state is not None:
        anchor_tool_call_id = ctx.narrator_state.last_tool_call_id
    if not update_ask:
        ctx.credential_pause_used = True
        ctx.credential_pause_anchor_tool_call_id = anchor_tool_call_id

    def settle(outcome: str) -> None:
        # An update card asks to fix a credential already chosen, so the turn keeps its pick card's state.
        if not update_ask:
            ctx.credential_pause_outcome = outcome

    cache = getattr(app, "CACHE", None)
    if cache is None:
        settle("declined")
        return None
    recovery_enabled = _credential_recovery_enabled(ctx)
    if not recovery_enabled and await stream.is_disconnected():
        settle("declined")
        return None
    policy = ctx.request_policy
    if update_credential_id is not None:
        credential_refs = [update_credential_id]
    else:
        # The FE credential card fetches the full org list itself; these ride the frame as `credential_refs`
        # and seed the picker's "Suggested" group (pinned first), so the user still sees the full list.
        if isinstance(policy, RequestPolicy):
            # Bind an answer to the URLs on this card, never an earlier unanswered ask.
            policy.credential_ask_login_page_urls = list(login_page_urls)
        credential_refs = list(policy.credential_refs) if isinstance(policy, RequestPolicy) else []
    timeout_seconds = copilot_config.credential_pause_timeout_seconds
    now = datetime.now(timezone.utc)

    organization_id = ctx.organization_id
    chat_id = ctx.workflow_copilot_chat_id or ""
    turn_id = ctx.turn_id or ""
    resume_token = _new_resume_token()
    expires_at = now + timedelta(seconds=timeout_seconds)
    manual_sign_in = None if update_ask or registration else _manual_sign_in_offer(ctx, login_page_urls)
    if manual_sign_in is not None or registration is not None:
        # Signing in or saving a generated credential can outlast the tab, so the card must survive a reload.
        ctx.credential_recovery_armed = True
        recovery_enabled = _credential_recovery_enabled(ctx)
    # Establish the active-pause record before the frame carries the token: the
    # response endpoint refuses to resolve a turn that has no pending record.
    # expires_at is the same deadline the frame tells the client -- the record's
    # own TTL is a separate infra grace, not a resolve-after-timeout allowance.
    card = WorkflowCopilotCredentialRequiredUpdate(
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
        anchor_tool_call_id=anchor_tool_call_id,
        sign_in_browser_session_id=manual_sign_in.browser_session_id if manual_sign_in else None,
        registration=registration,
        timestamp=now,
    )
    await cache.set(
        credential_pause_active_key(organization_id, chat_id, turn_id),
        _encode_active_pause(
            resume_token,
            expires_at,
            card=card if recovery_enabled else None,
            recovery_token_digest=ctx.credential_recovery_token_digest if recovery_enabled else None,
            manual_sign_in=manual_sign_in,
            registration=registration,
        ),
        ex=_longest_credential_pause_record_ttl()
        if manual_sign_in or registration
        else _credential_pause_record_ttl(timeout_seconds),
    )
    await stream.send(card)

    async def _invalidate_active_pause_record(
        *, keep_live: bool = False
    ) -> CredentialPauseResolution | None | Literal["live"]:
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
            raced_in = await _try_resolve_credential_response(response_key, organization_id, resume_token)
            if isinstance(raced_in, CredentialPauseResolution):
                return raced_in
            active_key = credential_pause_active_key(organization_id, chat_id, turn_id)
            record = _decode_active_pause(await cache.get(active_key))
            live_until = record.live_until() if record is not None and record.resume_token == resume_token else None
            if keep_live and live_until is not None and datetime.now(timezone.utc) < live_until:
                return "live"
            await cache.set(
                active_key,
                _encode_active_pause(
                    resume_token,
                    expires_at,
                    consumed=True,
                    registration=record.registration
                    if record is not None and record.resume_token == resume_token
                    else None,
                ),
                ex=_longest_credential_pause_record_ttl()
                if manual_sign_in or registration
                else _credential_pause_record_ttl(timeout_seconds),
            )
            return None

    response_key = credential_response_cache_key(organization_id, chat_id, turn_id)

    async def answered(
        outcome: CredentialPauseResolvedOutcome,
        credential: Credential | None = None,
        signed_in: SignedInProfile | None = None,
    ) -> None:
        # Reaches only the tab holding this turn's stream; a reloaded tab reads the turn-end credentialPause stamp.
        await stream.send(
            WorkflowCopilotCredentialPauseResolvedUpdate(
                turn_id=turn_id,
                workflow_copilot_chat_id=chat_id,
                resume_token=resume_token,
                outcome=outcome,
                credential_id=credential.credential_id if credential else None,
                name=credential.name if credential else signed_in.profile_name if signed_in else None,
                browser_profile_id=signed_in.browser_profile_id if signed_in else None,
                timestamp=datetime.now(timezone.utc),
            )
        )

    invalidated = False
    resolution: CredentialPauseResolution | None = None
    if not recovery_enabled and await stream.is_disconnected():
        # Legacy clients cannot restore a dropped frame, so preserve their
        # immediate decline when disconnect races the send.
        await _invalidate_active_pause_record()
        settle("declined")
        return None

    try:
        with pause_human_input(ctx, "credential"):
            wait_seconds: float = timeout_seconds
            while True:
                waited = await _wait_for_credential_response(
                    response_key, ctx, stream, wait_seconds, resume_token=resume_token
                )
                resolution = None if isinstance(waited, str) else waited
                if waited is not None:
                    break
                # The waiter's last look can race a sign-in start or a Done claim; settle that under the lock.
                await _record_turn_resumed(organization_id, chat_id, turn_id)
                settled = await _invalidate_active_pause_record(keep_live=True)
                if isinstance(settled, str):
                    wait_seconds = await _seconds_left_on_pause(ctx, resume_token)
                    continue
                resolution = settled
                invalidated = True
                break
    except BaseException:
        # Covers CancelledError (a direct BaseException subclass, not Exception)
        # alongside any unexpected failure in the wait loop itself -- the frame's
        # resume token is already live client-side either way, so the record must
        # not be left pending for the same reason as the disconnect/timeout case.
        # Can't act on a rescued resolution mid-unwind, only avoid corrupting state.
        await _invalidate_active_pause_record()
        raise
    await _record_turn_resumed(organization_id, chat_id, turn_id)

    if resolution is None and not invalidated:
        invalidated_resolution = await _invalidate_active_pause_record()
        resolution = invalidated_resolution if isinstance(invalidated_resolution, CredentialPauseResolution) else None
    generated_credential_id: str | None = None
    if registration is not None:
        settled_record = _decode_active_pause(
            await cache.get(credential_pause_active_key(organization_id, chat_id, turn_id))
        )
        if settled_record is None or settled_record.resume_token != resume_token:
            settled_record = None
        settled_registration = settled_record.registration if settled_record is not None else None
        generated_credential_id = settled_record.generated_credential_id if settled_record is not None else None
        if settled_registration is not None:
            ctx.credential_registration_outcome = settled_registration.outcome
            # A create claimed but never finished (the pause ran out mid-create) may still have saved a credential.
            connected = resolution is not None and resolution.action == "connected"
            if settled_registration.attempted and settled_registration.outcome is None and not connected:
                ctx.credential_registration_outcome = "unknown"
        if generated_credential_id is not None or ctx.credential_registration_outcome == "unknown":
            ctx.credential_generation_spent = True
    if resolution is None:
        settle("timeout")
        return None
    if resolution.action == "skip":
        settle("skipped")
        if not update_ask:
            # A missing_credential_run_failure pause means the diagnosed run left
            # last_test_ok=False; without clearing it, the resumed reply is intercepted
            # by the generic failed-test nudge instead of honoring the skip decision.
            ctx.last_test_ok = None
        await answered("skipped")
        return resolution

    if resolution.action == "signed_in":
        if resolution.signed_in is None or update_ask:
            settle("timeout")
            return None
        if isinstance(policy, RequestPolicy):
            _apply_signed_in_profile_to_policy(ctx, policy, resolution.signed_in)
        ctx.credential_pause_outcome = "signed_in"
        await answered("signed_in", signed_in=resolution.signed_in)
        return resolution

    credential = resolution.credential
    if credential is None:
        settle("timeout")
        return None
    if update_ask:
        # The update card grants no authority: the credential keeps its origin and the request policy is
        # unchanged. It has no picker, so an answer naming another credential is not this update.
        if credential.credential_id != update_credential_id:
            LOG.warning("copilot_credential_update_answer_names_another_credential")
            return None
        await answered("connected", credential)
        return resolution
    # A credential this card just generated has no saved site yet; the card's own origin is its only placement.
    is_generated = credential.credential_id == generated_credential_id
    if admit_connected is not None and not is_generated and not await admit_connected(credential):
        ctx.credential_pause_outcome = "not_admitted"
        await answered("not_admitted")
        return resolution
    if isinstance(policy, RequestPolicy):
        _apply_connected_credential_to_policy(ctx, policy, credential)
    ctx.credential_pause_outcome = "connected"
    await answered("connected", credential)
    return replace(resolution, generated=True) if is_generated else resolution


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
    ctx: CopilotContext,
    *,
    login_page_url: str,
    message: str,
    stream: EventSourceStream,
    copilot_config: CopilotConfig,
    update_credential_id: str | None = None,
    update_reason: Literal["credential_missing_totp", "credential_rejected_by_site"] = "credential_missing_totp",
    admit_connected: Callable[[Credential], Awaitable[bool]] | None = None,
    allow_second_ask: bool = False,
    anchor_tool_call_id: str | None = None,
    registration: WorkflowCopilotCredentialRegistration | None = None,
) -> CredentialPauseResolution | None:
    """Raise the card from the model's own ``request_credential`` call and wait, inline, for the
    answer, so tool calls the model issued alongside it can await ``credential_pause_settled``."""
    arm_credential_pause_gate(ctx)
    ctx.credential_ask_in_flight = True
    update_ask = update_credential_id is not None
    if update_ask:
        ctx.credential_totp_update_asked = True
    try:
        resolution = await _run_credential_pause(
            ctx,
            message,
            stream,
            copilot_config,
            reason=update_reason
            if update_ask
            else "credential_registration"
            if registration
            else "login_credentials_unresolved",
            login_page_urls=[login_page_url],
            update_credential_id=update_credential_id,
            admit_connected=admit_connected,
            allow_second_ask=allow_second_ask,
            anchor_tool_call_id=anchor_tool_call_id,
            registration=None if update_ask else registration,
        )
        if not update_ask:
            ctx.credential_pause_reaskable_by_run = resolution is None or resolution.action == "skip"
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
    bound = longest_credential_pause_seconds() + 30
    try:
        await asyncio.wait_for(settled.wait(), bound)
    except asyncio.TimeoutError:
        LOG.warning("copilot_credential_gate_wait_timed_out")
        settled.set()


async def maybe_credential_pause(
    ctx: CopilotContext,
    result: RunResultStreaming,
    stream: EventSourceStream,
    copilot_config: CopilotConfig,
) -> list[dict[str, Any]] | None:
    """Pause a finalizing turn that hit a typed run-derived credential ask.

    Returns the resume messages to re-enter the loop with, or None to let the
    caller finalize normally.
    """
    reason = credential_pause_reason(ctx)
    # A refused cross-site fill owns the card: it asks for its own origin, and a declined one is not asked again.
    if reason is None or ctx.credential_origin_recovery is not None:
        return None
    # Exactly True, not merely truthy: a partial stand-in returns a truthy
    # object for any attribute, which would hand the budget back on every turn.
    if ctx.credential_pause_reaskable_by_run is True:
        # The spent card was a guess the user never answered; this ask has a run behind it. Hand the
        # budget back once, so an unanswered guess cannot silently cost the turn its real card.
        ctx.credential_pause_reaskable_by_run = False
        ctx.credential_pause_used = False
    if not credential_pause_transport_ready(ctx, copilot_config):
        return None
    # Local import: same module-load-cycle reason as _assemble_resume_messages.
    from skyvern.forge.sdk.copilot.enforcement import _parse_normalized_final_response

    parsed = _parse_normalized_final_response(result)
    policy = ctx.request_policy
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
    if resolution.signed_in is not None:
        return _assemble_resume_messages(ctx, signed_in_facts(resolution.signed_in))
    credential = resolution.credential
    if credential is None:
        return None
    return _assemble_resume_messages(ctx, _connected_resume_text(credential))
