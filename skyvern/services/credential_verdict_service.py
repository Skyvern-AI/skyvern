"""Confirms a credential-rejection verdict before Skyvern returns one (SKY-18125).

Customers block a credential when a run comes back with `invalid_credentials`, so a wrong verdict is
expensive. Several detection paths can set that code off a single error-check model call, and the
confidence that call stores does not separate right verdicts from wrong ones. This module re-checks
the verdict against the evidence and, when it cannot be confirmed, replaces it with a code that says
so instead of one the customer acts on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

import structlog

from skyvern.config import settings
from skyvern.errors.errors import (
    AccountLockedError,
    CredentialCheck,
    CredentialCheckEvidence,
    LoginUnconfirmedError,
    UserDefinedError,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler_factory import get_org_aware_primary_llm_api_handler
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.utils.secret_redaction import collect_secret_values_at_any_length, redact_secrets_from_text

LOG = structlog.get_logger()

# Lower-cased error codes that mean "the website rejected the submitted credentials".
CREDENTIAL_REJECTION_CODES = frozenset(
    {
        "invalid_credentials",
        "invalid_credential",
        "incorrect_credentials",
        "wrong_credentials",
        "bad_credentials",
        "invalid_login",
        "failure_login_wrong_password",
    }
)

CONFIRM_PROMPT_NAME = "confirm-credential-rejection"
# Org-scoped: every run in an organization must resolve the same way, or a rollout cannot be read.
FEATURE_FLAG = "CONFIRM_INVALID_CREDENTIALS"


@dataclass(frozen=True)
class CredentialVerdictContext:
    """The evidence a caller already holds when it files a credential-rejection verdict. Everything is
    optional: a path that failed before the page was reachable still gets its verdict re-checked, it
    just has less to re-check it against."""

    current_url: str | None = None
    elements: str | None = None
    screenshots: list[bytes] | None = None
    action_history: str | None = None
    agent_reasoning: str | None = None


@dataclass(frozen=True)
class _ModelAnswer:
    site_rejected_credentials: bool | None
    account_locked_or_reset: bool
    site_message: str | None
    reasoning: str | None


@dataclass
class _Outcome:
    passed: list[CredentialCheck] = field(default_factory=list)
    failed: list[CredentialCheck] = field(default_factory=list)
    skipped: list[CredentialCheck] = field(default_factory=list)

    def record(self, check: CredentialCheck, result: bool | None) -> None:
        if result is None:
            self.skipped.append(check)
        elif result:
            self.passed.append(check)
        else:
            self.failed.append(check)


def is_credential_rejection_code(error_code: str) -> bool:
    return error_code.strip().lower() in CREDENTIAL_REJECTION_CODES


def needs_credential_confirmation(error: UserDefinedError) -> bool:
    """Lets a caller skip assembling page evidence it would only need for the confirmation call."""
    return is_credential_rejection_code(error.error_code) and not error.is_credential_check_done


async def confirm_credential_verdicts(
    *,
    task: Task,
    step: Step,
    errors: list[UserDefinedError],
    context: CredentialVerdictContext | None = None,
) -> list[UserDefinedError]:
    """Re-check every credential-rejection verdict in ``errors`` and return the list to report.

    Fails open in both directions that matter: an error code is never dropped, and a verdict is never
    marked confirmed on a check that did not run. A check with nothing to read is skipped rather than
    failed, so the gate downgrades a verdict only on evidence that contradicts it.
    """
    if not any(needs_credential_confirmation(error) for error in errors):
        return _strip_unverified_evidence(errors)
    if not await _is_enabled(task.organization_id):
        return _strip_unverified_evidence(errors)

    try:
        answer, history_logged_in_at, credential_known = await _gather_evidence(task=task, step=step, context=context)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.warning(
            "Credential verdict confirmation failed; reporting the verdict unconfirmed",
            task_id=task.task_id,
            step_id=step.step_id,
            exc_info=True,
        )
        answer, history_logged_in_at, credential_known = None, None, False

    outcome = _Outcome()
    outcome.record(
        CredentialCheck.SITE_REJECTED_CREDENTIALS, answer.site_rejected_credentials if answer is not None else None
    )
    outcome.record(
        CredentialCheck.NO_RECENT_LOGIN_WITH_SAME_CREDENTIAL,
        history_logged_in_at is None if credential_known else None,
    )
    # Needs the per-request status recording that item 5 of SKY-18125 adds; until then there is no
    # login-request status to read, and a check that did not run must not read as one that passed.
    outcome.record(CredentialCheck.LOGIN_REQUEST_NOT_UNAVAILABLE, None)

    confirmed = len(outcome.passed) == len(CredentialCheck)
    site_message = _redact_for_run(task, answer.site_message if answer is not None else None)
    evidence = CredentialCheckEvidence(
        confirmed=confirmed,
        checks_passed=list(outcome.passed),
        checks_failed=list(outcome.failed),
        checks_skipped=list(outcome.skipped),
        site_message=site_message or None,
    )

    replacement = _replacement(
        task=task,
        answer=answer,
        history_logged_in_at=history_logged_in_at,
        evidence=evidence,
    )

    confirmed_codes: list[str] = []
    downgraded_codes: list[str] = []
    reported: list[UserDefinedError] = []
    for error in errors:
        if not needs_credential_confirmation(error):
            reported.append(error.without_unverified_credential_check())
            continue
        if replacement is None:
            confirmed_codes.append(error.error_code)
            reported.append(error.with_credential_check(evidence))
            continue
        downgraded_codes.append(error.error_code)
        reported.append(replacement)

    LOG.info(
        "Credential verdict confirmation",
        task_id=task.task_id,
        step_id=step.step_id,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        confirmed=confirmed,
        confirmed_codes=confirmed_codes,
        downgraded_codes=downgraded_codes,
        replacement_code=replacement.error_code if replacement is not None else None,
        checks_passed=[str(check) for check in outcome.passed],
        checks_failed=[str(check) for check in outcome.failed],
        checks_skipped=[str(check) for check in outcome.skipped],
        model_answered=answer is not None,
        account_locked=answer.account_locked_or_reset if answer is not None else None,
        credential_known=credential_known,
        had_page_evidence=bool(context and (context.elements or context.screenshots)),
    )
    # Dedupe: two detection paths can file the same code, and both collapse onto one replacement.
    return _dedupe(reported)


def _replacement(
    *,
    task: Task,
    answer: _ModelAnswer | None,
    history_logged_in_at: datetime | None,
    evidence: CredentialCheckEvidence,
) -> UserDefinedError | None:
    """The error to report instead of the credential-rejection verdict, or None to keep it."""
    # A lock can follow a successful login, so it takes precedence over history evidence.
    if answer is not None and answer.account_locked_or_reset:
        return (
            AccountLockedError(reason=_redact_for_run(task, answer.reasoning) or None)
            .to_user_defined_error()
            .with_credential_check(evidence)
        )
    # A prior successful login contradicts bad credentials on its own, but not an account lock (handled above).
    if history_logged_in_at is not None:
        reason = (
            "the same credential completed a login on this workflow at "
            f"{history_logged_in_at.isoformat()}, which contradicts the rejection"
        )
        return LoginUnconfirmedError(reason=reason).to_user_defined_error().with_credential_check(evidence)
    # Never return this code unless every required check passed. The login-request status check is
    # not recorded yet, so enabled organizations conservatively receive an unconfirmed verdict.
    if evidence.confirmed:
        return None
    default_reason = (
        "the page shows no sign that the website evaluated the submitted username or password"
        if answer is not None and answer.site_rejected_credentials is False
        else "credential rejection could not be confirmed because one or more checks were inconclusive"
    )
    return (
        LoginUnconfirmedError(
            reason=_redact_for_run(task, answer.reasoning if answer is not None else None) or default_reason
        )
        .to_user_defined_error()
        .with_credential_check(evidence)
    )


def _strip_unverified_evidence(errors: list[UserDefinedError]) -> list[UserDefinedError]:
    return [error.without_unverified_credential_check() for error in errors]


def _dedupe(errors: list[UserDefinedError]) -> list[UserDefinedError]:
    seen: set[tuple[str, str]] = set()
    deduped: list[UserDefinedError] = []
    for error in errors:
        if error.error_code not in {"CREDENTIAL_LOGIN_UNCONFIRMED", "CREDENTIAL_ACCOUNT_LOCKED"}:
            deduped.append(error)
            continue
        identity = (error.error_code, error.reasoning)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(error)
    return deduped


async def _gather_evidence(
    *,
    task: Task,
    step: Step,
    context: CredentialVerdictContext | None,
) -> tuple[_ModelAnswer | None, datetime | None, bool]:
    answer, (credential_known, logged_in_at) = await asyncio.gather(
        _answer_or_none(task=task, step=step, context=context or CredentialVerdictContext()),
        _recent_login_or_unknown(task),
    )
    return answer, logged_in_at, credential_known


async def _answer_or_none(*, task: Task, step: Step, context: CredentialVerdictContext) -> _ModelAnswer | None:
    try:
        return await _ask_model(task=task, step=step, context=context)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.warning(
            "Credential rejection confirmation model call failed",
            task_id=task.task_id,
            step_id=step.step_id,
            exc_info=True,
        )
        return None


async def _recent_login_or_unknown(task: Task) -> tuple[bool, datetime | None]:
    try:
        return await _recent_login_with_same_credential(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.warning(
            "Credential login-history check failed",
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
            exc_info=True,
        )
        return False, None


async def _recent_login_with_same_credential(task: Task) -> tuple[bool, datetime | None]:
    """(whether a credential could be identified, when it last completed a login on this workflow)."""
    if not task.workflow_run_id or not task.organization_id:
        return False, None
    parameter_keys = _login_credential_parameter_keys(task)
    if not parameter_keys:
        return False, None
    since = datetime.now(timezone.utc) - timedelta(hours=settings.CREDENTIAL_VERDICT_LOGIN_HISTORY_HOURS)
    recent = await app.DATABASE.workflow_runs.get_recent_completed_login_for_run_credentials(
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        since=since,
        credential_parameter_keys=parameter_keys,
    )
    return recent.credential_known, recent.logged_in_at


def _login_credential_parameter_keys(task: Task) -> list[str]:
    """Only query history when this workflow has one unambiguous Login block."""
    manager = app.WORKFLOW_CONTEXT_MANAGER
    contexts = getattr(manager, "workflow_run_contexts", {})
    run_context = contexts.get(task.workflow_run_id) if isinstance(contexts, dict) and task.workflow_run_id else None
    workflow = getattr(run_context, "workflow", None)
    definition = getattr(workflow, "workflow_definition", None)
    if definition is None:
        return []

    from skyvern.forge.sdk.workflow.models.block import LoginBlock, get_all_blocks
    from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter, WorkflowParameterType

    login_blocks = [block for block in get_all_blocks(definition.blocks) if isinstance(block, LoginBlock)]
    if len(login_blocks) != 1:
        return []
    parameters = login_blocks[0].parameters
    return sorted(
        {
            parameter.key
            for parameter in parameters
            if parameter.key in settings.CREDENTIAL_VERDICT_HISTORY_PARAMETER_KEYS
        }
        | {
            parameter.key
            for parameter in parameters
            if isinstance(parameter, CredentialParameter)
            or getattr(parameter, "workflow_parameter_type", None) == WorkflowParameterType.CREDENTIAL_ID
        }
    )


async def _ask_model(*, task: Task, step: Step, context: CredentialVerdictContext) -> _ModelAnswer:
    code, description = _credential_code_under_review(task)
    prompt = prompt_engine.load_prompt(
        CONFIRM_PROMPT_NAME,
        credential_error_code=code,
        credential_error_description=description,
        navigation_goal=task.navigation_goal or "",
        agent_reasoning=context.agent_reasoning,
        action_history=context.action_history,
        elements=context.elements,
        current_url=context.current_url,
        local_datetime=datetime.now(_tz_info()).isoformat(),
    )
    response = await get_org_aware_primary_llm_api_handler(default=app.LLM_API_HANDLER)(
        prompt=prompt,
        screenshots=context.screenshots or [],
        step=step,
        prompt_name=CONFIRM_PROMPT_NAME,
        organization_id=task.organization_id,
    )
    if not isinstance(response, dict):
        raise ValueError(f"confirmation response is {type(response).__name__}, not a dict")
    return _ModelAnswer(
        site_rejected_credentials=_as_optional_bool(response.get("site_rejected_credentials")),
        account_locked_or_reset=_as_bool(response.get("account_locked_or_reset")),
        site_message=_as_text(response.get("site_message")),
        reasoning=_as_text(response.get("reasoning")),
    )


def _tz_info() -> tzinfo | None:
    context = skyvern_context.current()
    if context is not None and context.tz_info:
        return context.tz_info
    return datetime.now().astimezone().tzinfo


def _credential_code_under_review(task: Task) -> tuple[str | None, str | None]:
    for key, description in (task.error_code_mapping or {}).items():
        if is_credential_rejection_code(key):
            return key, description if isinstance(description, str) else None
    return None, None


def _as_bool(value: Any) -> bool:
    """Accept only JSON booleans at the model boundary."""
    if isinstance(value, bool):
        return value
    raise ValueError("confirmation answer must be a JSON boolean")


def _as_optional_bool(value: Any) -> bool | None:
    return None if value is None else _as_bool(value)


def _as_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _redact_for_run(task: Task, text: str | None) -> str | None:
    """Scrub the run's secrets out of model-authored text.

    The gate reads the page, so a password typed into the form can come back inside a quoted site
    message, and this text leaves over the customer webhook. Done here rather than at the callers
    because only some of them redact `reasoning`.
    """
    if not text:
        return text
    try:
        manager = app.WORKFLOW_CONTEXT_MANAGER
        run_contexts = getattr(manager, "workflow_run_contexts", {})
        run_context = run_contexts.get(task.workflow_run_id) if isinstance(run_contexts, dict) else None
        secrets = collect_secret_values_at_any_length(
            getattr(run_context, "secrets", {}),
            otp_values=getattr(run_context, "runtime_otp_values", ()),
        )
        context = skyvern_context.current()
        if context is not None:
            totp_values = [
                value
                for key, value in context.totp_codes.items()
                if isinstance(key, str) and not key.endswith(("_valid_from", "_valid_until")) and value is not None
            ]
            secrets |= collect_secret_values_at_any_length(
                {}, otp_values=[*context.runtime_secret_values, *totp_values]
            )
        if not secrets:
            return text
        return redact_secrets_from_text(
            text,
            secrets,
            placeholder_ids=manager.registered_placeholder_ids_for_run(task.workflow_run_id),
        )
    except Exception:
        LOG.warning(
            "Could not redact credential confirmation text; dropping it",
            task_id=task.task_id,
            exc_info=True,
        )
        return None


async def _is_enabled(organization_id: str | None) -> bool:
    if not organization_id or not app.EXPERIMENTATION_PROVIDER:
        return False
    try:
        return bool(
            await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                FEATURE_FLAG,
                organization_id,
                properties={"organization_id": organization_id},
            )
        )
    except Exception:
        LOG.warning(f"Failed to resolve {FEATURE_FLAG} feature flag", exc_info=True)
        return False
