import re
from collections.abc import Collection
from datetime import timedelta

import structlog
from fastapi import BackgroundTasks, Depends, HTTPException, Query, Request, status

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.log_redaction import redact_bearer_tokens_in_text
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.feedback import (
    AgentFeedback,
    AgentFeedbackRequest,
    AgentFeedbackResponse,
    FeedbackEvent,
    RunFeedback,
    RunFeedbackRequest,
    RunFeedbackTargetType,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.services.request_principal import get_request_principal
from skyvern.utils.secret_redaction import redact_secrets_from_text

LOG = structlog.get_logger()


async def _enforce_target_folder_access(
    target_type: RunFeedbackTargetType, target_id: str, organization_id: str, action: str
) -> None:
    """Reading feedback needs read access to the target's workflow folder; submitting it needs write access."""
    if target_type == "workflow_run":
        resource: dict[str, object] = {"workflow_run_id": target_id}
    elif target_type == "browser_recording":
        recording = await app.DATABASE.browser_recordings.get_recording(target_id, organization_id)
        if recording is None or recording.workflow_permanent_id is None:
            return
        resource = {"workflow_permanent_id": recording.workflow_permanent_id}
    else:
        resource = {"task_id": target_id}
    await app.AGENT_FUNCTION.enforce_folder_access(get_request_principal(), action, resource)


async def _resolve_target_context(
    target_type: RunFeedbackTargetType, target_id: str, organization_id: str
) -> str | None:
    """Return the target's parent id, or 404 when the target is not visible to the organization."""
    if target_type == "workflow_run":
        workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
            workflow_run_id=target_id, organization_id=organization_id
        )
        if workflow_run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        return workflow_run.workflow_permanent_id
    if target_type == "browser_recording":
        recording = await app.DATABASE.browser_recordings.get_recording(target_id, organization_id)
        if recording is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found")
        return recording.workflow_permanent_id
    task = await app.DATABASE.tasks.get_task(target_id, organization_id=organization_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return task.workflow_run_id


def _is_unchanged(previous: RunFeedback | None, request: RunFeedbackRequest) -> bool:
    if previous is None:
        return request.rating is None
    return (previous.rating, previous.reason, previous.needs_support) == (
        request.rating,
        request.reason or None,
        request.needs_support,
    )


async def _fan_out(organization: Organization, event: FeedbackEvent) -> None:
    try:
        await app.AGENT_FUNCTION.on_feedback_submitted(organization=organization, event=event)
    except Exception:
        LOG.exception(
            "Feedback fan-out failed",
            organization_id=organization.organization_id,
            target_type=event.target_type,
            target_id=event.target_id,
        )


@base_router.post("/feedback", response_model=RunFeedback | None, include_in_schema=False)
async def submit_run_feedback(
    request: RunFeedbackRequest,
    background_tasks: BackgroundTasks,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> RunFeedback | None:
    organization_id = current_org.organization_id
    # Submitting writes feedback and notifies people, so a folder viewer cannot do it.
    await _enforce_target_folder_access(request.target_type, request.target_id, organization_id, "workflow_run.update")
    context_id = await _resolve_target_context(request.target_type, request.target_id, organization_id)
    previous = await app.DATABASE.run_feedback.get_run_feedback(
        organization_id=organization_id, target_type=request.target_type, target_id=request.target_id
    )

    if request.rating is None:
        await app.DATABASE.run_feedback.delete_run_feedback(
            organization_id=organization_id, target_type=request.target_type, target_id=request.target_id
        )
        feedback = None
    else:
        feedback = await app.DATABASE.run_feedback.upsert_run_feedback(
            organization_id=organization_id,
            target_type=request.target_type,
            target_id=request.target_id,
            context_id=context_id,
            rating=request.rating,
            reason=request.reason,
            needs_support=request.needs_support,
            submitted_by=request.submitted_by,
        )

    # A re-save of the same state (a retried request, a double click) is not a new signal for sinks.
    if _is_unchanged(previous, request):
        return feedback

    event = FeedbackEvent(
        target_type=request.target_type,
        target_id=request.target_id,
        context_id=context_id,
        rating=request.rating,
        previous_rating=previous.rating if previous else None,
        reason=request.reason,
        needs_support=request.needs_support,
        submitted_by=request.submitted_by,
    )
    # Sinks (Slack, analytics) run after the response so a slow vendor cannot hold the request open.
    background_tasks.add_task(_fan_out, current_org, event)
    return feedback


@base_router.get("/feedback", response_model=RunFeedback | None, include_in_schema=False)
async def get_run_feedback(
    target_type: RunFeedbackTargetType = Query(...),
    target_id: str = Query(..., min_length=1, max_length=128),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> RunFeedback | None:
    await _enforce_target_folder_access(target_type, target_id, current_org.organization_id, "workflow_run.read")
    return await app.DATABASE.run_feedback.get_run_feedback(
        organization_id=current_org.organization_id, target_type=target_type, target_id=target_id
    )


_PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN ([A-Z ]*PRIVATE KEY(?: BLOCK)?)-----.*?(?:-----END \1-----|\Z)", re.S)
_SECRET_ASSIGNMENT = re.compile(
    r"(?<![\w-])((?:Set-)?Cookie:[^\S\r\n]*)(?=[^\s=;]+[^\S\r\n]*=)[^\r\n]*"
    r"|(?<![\w.-])((?:\\?[\"'])?[\w.-]*(?:password|passwd|passcode|secret|client_secret|api[_ -]?key|token|access_token"
    r"|refresh_token|(?:secret|signing|encryption|master|private)[_ -]?key|authorization|cookie)(?:\\?[\"'])?\s*[:=]\s*)"
    r"(?:\\\"(?:\\\\(?:\\.|[^\\])|\\[^\"\\]|[^\"\\])*\\\"|\\'(?:\\\\(?:\\.|[^\\])|\\[^'\\]|[^'\\])*\\'"
    r"|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
    r"|(?<![\w-])(--(?:password|passwd|token|api[-_]?key|secret|client-secret)(?:\s+|=\s*))"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|\S+)"
    r"|(?<![\w.-])((?:[\w.-]*(?:password|passwd|(?:secret|signing|encryption|master|private)[_ -]?key)"
    r"|[\w.-]*[_.-]pwd)\s*=\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|\S+)"
    r"|(?<![\w.-])([\w.-]*(?:secret|token|cookie)\s*=\s*)(?:(?:bearer|basic|token|digest)\s+)?\S+"
    r"|(?<![\w.-])([\w.-]*(?:(?-i:Password|Passwd|Passcode|Secret|Token|[Aa]piKey"
    r"|(?:[Ss]ecret|[Ss]igning|[Ee]ncryption|[Mm]aster|[Pp]rivate)Key)"
    r"|(?:secret|signing|encryption|master|private)[_ -]key)\s*:\s*)"
    r"(?:(?:bearer|basic|token|digest)\s+)?(?:\[REDACTED_SECRET\]|[^\s,;}\])\"'])+",
    re.I | re.S,
)
_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/:@?#]*:[^\s/@?#]*@")
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_AWS_SECRET_ASSIGNMENT = re.compile(
    r"(?<![\w.-])([\"']?(?:[\w.-]*[_.-])?secret_access_key[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;\"'}]+)",
    re.I | re.S,
)
_VENDOR_TOKEN = re.compile(
    r"\b(?:(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+|xox[abposr]-[A-Za-z0-9-]+"
    r"|[sr]k_(?:live|test)_[A-Za-z0-9]+|AIza[A-Za-z0-9_-]{35})(?![\w])"
)
_PAYMENT_CARD = re.compile(r"[0-9]++(?:[ -][0-9]++)*+")
_PAYMENT_CARD_GROUP = re.compile(r"[0-9]+")


def _redact_payment_card(match: re.Match[str]) -> str:
    value = match.group()
    digits = value.replace(" ", "").replace("-", "")
    supported = (
        (digits.startswith("4") and len(digits) in (13, 16, 19))
        or (len(digits) == 16 and (51 <= int(digits[:2]) <= 55 or 2221 <= int(digits[:4]) <= 2720))
        or (len(digits) == 15 and digits.startswith(("34", "37")))
        or (
            16 <= len(digits) <= 19
            and (
                digits.startswith(("6011", "65"))
                or 644 <= int(digits[:3]) <= 649
                or 622126 <= int(digits[:6]) <= 622925
            )
        )
    )
    if not supported:
        return value
    checksum = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2:
            number *= 2
            if number > 9:
                number -= 9
        checksum += number
    return "[REDACTED_SECRET]" if checksum % 10 == 0 else value


def _redact_report_text(text: str, known_values: Collection[str]) -> str:
    text = redact_secrets_from_text(text, known_values, match_inside_words=True)
    text = _PRIVATE_KEY_BLOCK.sub("[REDACTED_SECRET]", text)
    # Keep the retained keys outside the legacy whole-assignment redactor.
    parts = []
    position = 0
    for match in _SECRET_ASSIGNMENT.finditer(text):
        parts.append(redact_raw_secrets_for_prompt(text[position : match.start()]))
        key = next(group for group in match.groups() if group is not None)
        parts.append(f"{key}[REDACTED_SECRET]")
        position = match.end()
    parts.append(redact_raw_secrets_for_prompt(text[position:]))
    text = "".join(parts)
    text = redact_bearer_tokens_in_text(text)
    text = _URL_USERINFO.sub(r"\1[REDACTED_SECRET]@", text)
    text = _AWS_ACCESS_KEY.sub("[REDACTED_SECRET]", text)
    text = _AWS_SECRET_ASSIGNMENT.sub(r"\1[REDACTED_SECRET]", text)
    text = _VENDOR_TOKEN.sub("[REDACTED_SECRET]", text)

    def redact_card_run(match: re.Match[str]) -> str:
        value = match.group()
        groups = list(_PAYMENT_CARD_GROUP.finditer(value))
        if match.start() and text[match.start() - 1] == "_" and len(groups[0].group()) >= 18:
            groups = groups[1:]
        if groups and match.end() < len(text) and text[match.end()] == "_" and len(groups[-1].group()) >= 18:
            groups = groups[:-1]
        parts = []
        start = 0
        position = 0
        while start < len(groups):
            candidates = []
            digit_count = 0
            for end in range(start, len(groups)):
                digit_count += len(groups[end].group())
                if digit_count > 19:
                    break
                if digit_count >= 13:
                    candidates.append(end)
            for end in reversed(candidates):
                candidate = _PAYMENT_CARD.fullmatch(value[groups[start].start() : groups[end].end()])
                if candidate is not None and _redact_payment_card(candidate) == "[REDACTED_SECRET]":
                    parts.append(value[position : groups[start].start()])
                    parts.append("[REDACTED_SECRET]")
                    position = groups[end].end()
                    start = end + 1
                    break
            else:
                start += 1
        parts.append(value[position:])
        return "".join(parts)

    return _PAYMENT_CARD.sub(redact_card_run, text)


async def _fan_out_agent_feedback(organization: Organization, feedback: AgentFeedback, redacted: bool) -> None:
    try:
        await app.AGENT_FUNCTION.on_agent_feedback_submitted(
            organization=organization, feedback=feedback, redacted=redacted
        )
    except Exception:
        LOG.error(
            "Agent feedback fan-out failed",
            organization_id=organization.organization_id,
            agent_feedback_id=feedback.agent_feedback_id,
        )


@base_router.post("/agent_feedback", response_model=AgentFeedbackResponse, include_in_schema=False)
async def submit_agent_feedback(
    request: AgentFeedbackRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> AgentFeedbackResponse:
    organization_id = current_org.organization_id
    count = await app.DATABASE.agent_feedback.count_agent_feedback_since(
        organization_id, naive_utc_now() - timedelta(minutes=60)
    )
    limit = settings.AGENT_FEEDBACK_HOURLY_LIMIT
    if count >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Feedback limit reached: this organization sent {limit} feedback reports in the last hour, "
                "which is the limit. This report was not stored. Do not retry it now."
            ),
        )

    known_values = []
    api_key = http_request.headers.get("x-api-key", "")
    if len(api_key) >= 8:
        known_values.append(api_key)
    authorization = http_request.headers.get("authorization", "").split()
    if len(authorization) == 2 and authorization[0].lower() == "bearer" and len(authorization[1]) >= 8:
        known_values.append(authorization[1])

    summary = _redact_report_text(request.summary, known_values)
    details = _redact_report_text(request.details, known_values) if request.details is not None else None
    tool_name = _redact_report_text(request.tool_name, known_values) if request.tool_name is not None else None
    original_context = request.context.model_dump() if request.context is not None else None
    context = (
        {
            key: _redact_report_text(value, known_values) if value is not None else None
            for key, value in original_context.items()
        }
        if original_context is not None
        else None
    )
    redacted = (summary, details, tool_name, context) != (
        request.summary,
        request.details,
        request.tool_name,
        original_context,
    )
    feedback = await app.DATABASE.agent_feedback.create_agent_feedback(
        organization_id=organization_id,
        category=request.category,
        summary=summary,
        severity=request.severity,
        details=details,
        tool_name=tool_name,
        context=context,
    )
    background_tasks.add_task(_fan_out_agent_feedback, current_org, feedback, redacted)
    return AgentFeedbackResponse(agent_feedback_id=feedback.agent_feedback_id, redacted=redacted)
