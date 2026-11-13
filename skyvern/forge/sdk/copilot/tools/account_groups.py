from __future__ import annotations

import asyncio
import hmac
import json
from collections import Counter
from collections.abc import Awaitable, Iterable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, TypeVar
from uuid import uuid4

import structlog
from fastapi import HTTPException
from pydantic import BaseModel, Field, JsonValue, ValidationError

from skyvern.config import settings
from skyvern.exceptions import MissingBrowserAddressError, SkyvernHTTPException
from skyvern.forge import app
from skyvern.forge.agent_functions import AuditEvent
from skyvern.forge.sdk.copilot.ask_user import (
    REPEAT_RISK_OUTCOMES,
    SINGLE_RUN_TOOL_NAME,
    AccountGroupCancelReview,
    AccountGroupReview,
    AccountGroupRow,
    AccountGroupRunRow,
    QuestionInteraction,
    RunCostFacts,
    wait_for_interaction,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.cost_telemetry import exclude_workflow_block_cost
from skyvern.forge.sdk.copilot.credential_resolution import load_credentials
from skyvern.forge.sdk.copilot.proposal_accept import accept_copilot_proposal
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.secret_scrub import (
    REDACTED_SECRET_PLACEHOLDER,
    scrub_secrets_from_structure,
    scrub_secrets_from_text,
)
from skyvern.forge.sdk.copilot.tools._shared import _executable_workflow_block_labels
from skyvern.forge.sdk.copilot.tools.credentials import (
    _credential_run_approval_error,
    _extract_credential_ids_from_tool_value,
    _extract_credential_ids_from_workflow_parameters,
    run_credential_gate,
)
from skyvern.forge.sdk.copilot.tools.mcp_hooks import PENDING_PROPOSAL_FACT, _chat_has_pending_proposal
from skyvern.forge.sdk.copilot.tools.run_execution import _declared_execution_snapshot, _run_credits_used
from skyvern.forge.sdk.copilot.tools.workflow_update import adopt_stored_workflow_candidate
from skyvern.forge.sdk.copilot.workflow_credential_utils import (
    block_credential_ids,
    credential_param_ids,
    declared_parameters,
    parse_workflow_yaml,
    workflow_blocks,
    workflow_connection_refs,
)
from skyvern.forge.sdk.copilot.workflow_yaml import workflow_to_copilot_yaml
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.permissions.permission_checker_factory import PermissionCheckerFactory
from skyvern.forge.sdk.core.run_submission_gate import run_submission_slot
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.db.id import generate_workflow_run_id
from skyvern.forge.sdk.schemas.credentials import Credential
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflow_copilot import (
    CopilotProposalMetadata,
    WorkflowCopilotChat,
    copilot_proposal_metadata,
)
from skyvern.forge.sdk.services.request_principal import (
    RequestPrincipal,
    enforce_principal_role,
    get_request_principal,
    trigger_owner_id,
)
from skyvern.forge.sdk.workflow.block_notes import strip_inert_block_fields
from skyvern.forge.sdk.workflow.models.parameter import (
    CredentialParameter,
    WorkflowParameter,
    WorkflowParameterType,
    is_sensitive_workflow_parameter,
)
from skyvern.forge.sdk.workflow.models.tags import CallerType
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRequestBody,
    WorkflowRun,
    WorkflowRunParameter,
    WorkflowRunStatus,
)
from skyvern.schemas.run_enums import RunType
from skyvern.schemas.workflow_run_groups import (
    WorkflowRunGroupCreateRequest,
    WorkflowRunGroupItemOutcome,
    WorkflowRunGroupItemRequest,
    WorkflowRunGroupResponse,
    WorkflowRunGroupStatus,
)
from skyvern.schemas.workflows import BlockStatus
from skyvern.services import workflow_run_group_service, workflow_service
from skyvern.services.organization_log_scope import organization_log_scope

LOG = structlog.get_logger()

_T = TypeVar("_T")

ACCOUNT_GROUP_MAX_ITEMS = 25

SUBMIT_TOOL_DESCRIPTION = (
    "Run the saved workflow once per selected saved account, one run after another, each in a fresh "
    "browser."
    "\n\n"
    "Use this when the user wants the same saved workflow run for several of their saved accounts. Pass "
    "the credential ids the user selected (from list_credentials), the saved workflow's credential_id "
    "parameter that takes the account, the other inputs every run shares, and a one-sentence summary of "
    "what each run does. The user reviews the accounts on a card, can remove any, and approves or "
    "declines; nothing runs without approval. At most 25 accounts per group. The card shows whether this "
    "saved version has completed cleanly in an earlier group for a selected account, and starts unchecked "
    "any account whose last group run completed, may have acted, or is still running. To retry part of an "
    "earlier group, propose those accounts again. The result only acknowledges dispatch; use "
    "get_account_group_status for outcomes."
)

SINGLE_RUN_TOOL_DESCRIPTION = (
    "Start one real run of the saved workflow, the same as the user pressing Run. It is not a test: it runs the "
    "saved version with the workflow's own browser profile and run settings, and it keeps running after your "
    "reply. Nothing runs until the user approves it on a card that lists the inputs you pass and the saved account "
    "each credential_id input uses; credentials set inside the workflow run as saved. Inputs you leave out use "
    "their saved defaults; a credential_id input with neither runs without a credential, as Run does. When this chat "
    "has an unsaved workflow change, the card says so, and the user's approval saves that change as a new version "
    "and starts the run on it; a change to a saved login, connected account or browser profile is saved only by the "
    "user accepting it. The result gives the workflow_run_id; get_run_results with that id reads the run's progress "
    "and outcome."
)

_UNFINISHED_OUTCOMES = frozenset({WorkflowRunGroupItemOutcome.pending, WorkflowRunGroupItemOutcome.in_progress})

_UNCLEAN_BLOCK_STATUSES = frozenset(
    {BlockStatus.failed, BlockStatus.terminated, BlockStatus.timed_out, BlockStatus.canceled}
)

# Every run setting an in-place accept writes, except what the engine rewrites after runs: run_with, cache_key,
# code_version, browser_profile_id (set on the first persisted run) and title (default-title rename).
_VERIFIED_WORKFLOW_FIELDS = frozenset(
    {
        "workflow_definition",
        "proxy_location",
        "webhook_callback_url",
        "totp_verification_url",
        "totp_identifier",
        "persist_browser_session",
        "reuse_browser_session",
        "mask_secrets",
        "pin_saved_session_ip",
        "browser_profile_key",
        "model",
        "max_screenshot_scrolls",
        "max_elapsed_time_minutes",
        "extra_http_headers",
        "cdp_connect_headers",
        "ai_fallback",
        "adaptive_caching",
        "enable_self_healing",
        "run_sequentially",
        "sequential_key",
    }
)

_NON_LOGIN_PARAMETER_TYPES = frozenset({"workflow", "output", "context"})
_PARAMETER_ROW_FIELDS = frozenset({"workflow_id", "created_at", "modified_at", "deleted_at"})

_DEFINITION_HASH_DOMAIN = b"skyvern.copilot_account_group_definition.v1\0"
_IDENTITY_HASH_DOMAIN = b"skyvern.copilot_account_group_identity.v1\0"
_ACCOUNT_GROUP_KEY_ROOT = "copilot:"

_PENDING_PROPOSAL_REFUSAL = f"Nothing was submitted: {PENDING_PROPOSAL_FACT}"

_SINGLE_RUN_PENDING_PROPOSAL_REFUSAL = f"Nothing was started: {PENDING_PROPOSAL_FACT}"

_SINGLE_RUN_BINDING_REFUSAL = (
    "Nothing was started: this tool runs the saved version, and this chat has an unsaved change to which saved "
    "login the agent uses. The user saves it by accepting it, or keeps the saved version by rejecting it."
)

_SINGLE_RUN_STALE_VERSION_REFUSAL = "The saved workflow was updated after the review, so nothing was started."

_SINGLE_RUN_DRAFT_REJECTED_REFUSAL = (
    "Nothing was saved or started: the workflow change was rejected after the review, and the saved version is "
    "unchanged."
)

_SINGLE_RUN_STALE_DRAFT_REFUSAL = (
    "Nothing was saved or started: the workflow change or the saved workflow changed after the review. "
    "Propose the run again so the user can review the current version."
)

_SINGLE_RUN_NOTE = "The run is not tied to this turn and keeps going after it ends."

_SINGLE_RUN_UNCONFIRMED = (
    "Starting the run raised an error after its run id was created, so the run may or may not exist."
)

_QUOTA_UNREADABLE_NOTE = "The account's credits could not be read just now, so no credit numbers are shown."

_STATUS_NOTE = (
    "Runs execute one after another. Pending and in-progress rows have no result yet; "
    "use get_account_group_status for outcomes."
)


class AccountGroupRefused(Exception):
    pass


class AccountGroupRefusal(BaseModel):
    ok: Literal[False] = False
    error: str
    status_code: int | None = None
    dispatched: Literal[False] | None = None


class AccountGroupSubmission(BaseModel):
    ok: Literal[True] = True
    approved: bool
    dispatched: bool
    workflow_run_group_id: str | None = None
    clean_group_run_id: str | None = None
    credential_parameter_used_by: list[str] = Field(default_factory=list)
    rows: list[AccountGroupRunRow] = Field(default_factory=list)
    repeated_after_prior_effect: list[AccountGroupRow] = Field(default_factory=list)
    cost: RunCostFacts | None = Field(default=None, exclude_if=lambda cost: cost is None)
    note: str


class SingleRunSubmission(BaseModel):
    ok: bool = True
    approved: bool
    dispatched: bool | None
    workflow_run_id: str | None = None
    saved_version: int | None = None
    error: str | None = None
    note: str | None = None
    cost: RunCostFacts | None = Field(default=None, exclude_if=lambda cost: cost is None)


class AccountGroupState(BaseModel):
    ok: Literal[True] = True
    workflow_run_group_id: str
    status: WorkflowRunGroupStatus
    rows: list[AccountGroupRunRow]
    cancel_approved: bool | None = None


async def _require_run_role() -> None:
    try:
        await enforce_principal_role(get_request_principal(), "workflow.run")
    except HTTPException as e:
        if e.status_code != 403:
            raise AccountGroupRefused("Nothing was started: the run permission could not be checked just now.") from e
        raise AccountGroupRefused(
            "Nothing was started: the person in this chat does not have permission to run workflows."
        ) from e


async def _require_save_permission(chat_id: str) -> None:
    principal = get_request_principal()
    try:
        await enforce_principal_role(principal, "workflow.update")
        await app.AGENT_FUNCTION.enforce_folder_access(
            principal, "workflow.update", {"workflow_copilot_chat_id": chat_id}
        )
    except HTTPException as e:
        if e.status_code != 403:
            raise AccountGroupRefused(
                "Nothing was saved or started: the save permission could not be checked just now."
            ) from e
        raise AccountGroupRefused(
            "Nothing was saved or started: the person in this chat does not have permission to save this workflow."
        ) from e


def single_run_submit_enabled() -> bool:
    return settings.COPILOT_ACCOUNT_GROUP_SUBMIT_ENABLED


def account_group_submit_enabled() -> bool:
    return settings.COPILOT_ACCOUNT_GROUP_SUBMIT_ENABLED and settings.WORKFLOW_RUN_GROUPS_SUBMIT_ENABLED


def workflow_definition_hash(workflow: Workflow) -> str:
    fields = strip_inert_block_fields(workflow.model_dump(mode="json", include=set(_VERIFIED_WORKFLOW_FIELDS)))
    # The model serializer masks every cdp_connect_headers value, so a changed header would hash the same.
    fields["cdp_connect_headers"] = workflow.cdp_connect_headers
    # Keyed because the digest leaves the server on the review card and covers raw header values.
    message = _DEFINITION_HASH_DOMAIN + json.dumps(fields, sort_keys=True).encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, sha256).hexdigest()


def _identity_digest(credential: Credential) -> str:
    # The masked label can hide a username change, so the review binds the exact name and username it showed.
    message = _IDENTITY_HASH_DOMAIN + json.dumps([credential.name, credential.username]).encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, sha256).hexdigest()


def account_group_key_prefix(chat_id: str) -> str:
    return f"{_ACCOUNT_GROUP_KEY_ROOT}{chat_id}:"


def account_group_submission_key(chat_id: str, interaction_id: str, definition_hash: str) -> str:
    return f"{account_group_key_prefix(chat_id)}{interaction_id}:{definition_hash}"


def _approved_request(
    chat_id: str, interaction_id: str, review: AccountGroupReview, approved_ids: list[str]
) -> WorkflowRunGroupCreateRequest:
    return WorkflowRunGroupCreateRequest(
        workflow_id=review.workflow_permanent_id,
        version=review.version,
        submission_key=account_group_submission_key(chat_id, interaction_id, review.definition_hash),
        items=[
            WorkflowRunGroupItemRequest(
                key=credential_id,
                parameters={**review.common_inputs, review.credential_parameter_key: credential_id},
            )
            for credential_id in approved_ids
        ],
        max_concurrency=1,
    )


async def recover_account_group_links(
    organization_id: str, chat_id: str, interactions: Iterable[QuestionInteraction]
) -> None:
    pending: list[tuple[AccountGroupReview, WorkflowRunGroupCreateRequest]] = []
    for interaction in interactions:
        review = interaction.account_group_review
        decision = interaction.account_group_decision
        if (
            review is None
            or review.single_run
            or review.workflow_run_group_id is not None
            or decision is None
            or not decision.approved
        ):
            continue
        try:
            request = _approved_request(chat_id, interaction.interaction_id, review, decision.credential_ids)
        except Exception:
            LOG.warning(
                "Skipping an approved review whose request cannot be rebuilt",
                interaction_id=interaction.interaction_id,
                exc_info=True,
            )
            continue
        pending.append((review, request))
    if not pending:
        return
    try:
        groups = await app.DATABASE.workflow_run_groups.get_groups_by_submission_keys(
            organization_id, [request.submission_key for _, request in pending]
        )
    except Exception:
        LOG.warning("Failed to recover the account groups of approved reviews", chat_id=chat_id, exc_info=True)
        return
    for review, request in pending:
        group = groups.get(request.submission_key)
        if group is not None and hmac.compare_digest(
            group.input_fingerprint, workflow_run_group_service.submission_fingerprint(request)
        ):
            review.workflow_run_group_id = group.workflow_run_group_id


def _mask_username(username: str) -> str:
    local, at, domain = username.partition("@")
    return f"{local[:2]}***{at}{domain}"


def _label(credential: Credential) -> str:
    return f"{credential.name} ({_mask_username(credential.username)})" if credential.username else credential.name


async def _saved_workflow(ctx: CopilotContext) -> Workflow:
    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=ctx.workflow_permanent_id, organization_id=ctx.organization_id
    )
    if workflow is None or not workflow.workflow_definition.blocks:
        raise AccountGroupRefused("This workflow has no saved version with blocks to run.")
    return workflow


async def _require_credential_parameter(workflow: Workflow, key: str) -> WorkflowParameter:
    parameters = await app.WORKFLOW_SERVICE.get_workflow_parameters(workflow_id=workflow.workflow_id)
    credential_parameters = {
        p.key: p for p in parameters if p.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID
    }
    if key not in credential_parameters:
        raise AccountGroupRefused(
            f"The saved workflow has no credential_id parameter named {key!r} "
            f"(its credential_id parameters: {list(credential_parameters) or 'none'}). Nothing was submitted. Add a "
            "credential_id input that takes the account, use it in the blocks that sign in, and have the user "
            "accept that change."
        )
    return credential_parameters[key]


async def _clean_group_run_id(
    ctx: CopilotContext, workflow: Workflow, parameter: WorkflowParameter, credential_ids: list[str]
) -> str | None:
    return await app.DATABASE.workflow_runs.get_latest_group_child_run_id(
        organization_id=ctx.organization_id,
        workflow_permanent_id=workflow.workflow_permanent_id,
        workflow_id=workflow.workflow_id,
        workflow_parameter_id=parameter.workflow_parameter_id,
        values=credential_ids,
        submission_key_suffix=f":{workflow_definition_hash(workflow)}",
        completed_without_block_statuses=_UNCLEAN_BLOCK_STATUSES,
    )


def _blocks_using_parameter(workflow: Workflow, key: str) -> list[str]:
    parsed = parse_workflow_yaml(workflow_to_copilot_yaml(workflow))
    return [
        str(block.get("label"))
        for block in workflow_blocks(parsed if isinstance(parsed, dict) else {})
        if key in (block.get("parameter_keys") or [])
    ]


async def _latest_outcomes(
    ctx: CopilotContext, workflow: Workflow, credential_ids: list[str]
) -> tuple[dict[str, str], dict[str, WorkflowRunGroupItemOutcome]]:
    latest = await app.DATABASE.workflow_run_groups.get_latest_group_ids_by_item_key(
        ctx.organization_id, workflow.workflow_permanent_id, _ACCOUNT_GROUP_KEY_ROOT, credential_ids
    )
    groups = await asyncio.gather(
        *(
            workflow_run_group_service.get_workflow_run_group(group_id, ctx.organization_id)
            for group_id in set(latest.values())
        )
    )
    outcomes = {
        item.key: item.outcome
        for group in groups
        for item in group.items
        if latest.get(item.key) == group.workflow_run_group_id
    }
    return latest, outcomes


async def _resolve_credentials(ctx: CopilotContext, credential_ids: list[str]) -> dict[str, Credential]:
    found = {
        credential.credential_id: credential
        for credential in await load_credentials(ctx.organization_id, credential_ids=credential_ids)
    }
    missing = [credential_id for credential_id in credential_ids if credential_id not in found]
    if missing:
        raise AccountGroupRefused(f"These credential ids are not saved accounts in this organization: {missing}.")
    return found


async def _organization(ctx: CopilotContext) -> Organization:
    organization = await app.DATABASE.organizations.get_organization(ctx.organization_id)
    if organization is None:
        raise AccountGroupRefused("Organization not found.")
    return organization


def _check_common_inputs(ctx: CopilotContext, common_inputs: dict[str, JsonValue]) -> None:
    if scrub_secrets_from_structure(ctx, common_inputs) != common_inputs:
        raise AccountGroupRefused(
            "A common input holds a secret value, and secrets cannot be group inputs. Nothing was submitted. "
            "Use a saved credential for the secret instead."
        )
    approval_error = _credential_run_approval_error(
        _extract_credential_ids_from_tool_value(common_inputs), ctx.request_policy
    )
    if approval_error is not None:
        raise AccountGroupRefused(approval_error)


async def _check_connected_accounts(
    ctx: CopilotContext,
    workflow: Workflow,
    common_inputs: dict[str, JsonValue],
    *,
    action: str = "run the workflow for these accounts",
    blocked_tool: str = "run_workflow_for_accounts",
) -> None:
    gate = await run_credential_gate(
        ctx,
        workflow.workflow_definition,
        labels=None,
        run_parameters=dict(common_inputs),
        action=action,
        blocked_tool=blocked_tool,
    )
    if gate.error is not None:
        raise AccountGroupRefused(gate.error)


async def _check_draft_connected_accounts(
    ctx: CopilotContext, draft: Workflow, common_inputs: dict[str, JsonValue]
) -> None:
    await _check_connected_accounts(
        ctx, draft, common_inputs, action="save and run the workflow change", blocked_tool=SINGLE_RUN_TOOL_NAME
    )


async def _fact(name: str, read: Awaitable[_T]) -> _T | None:
    try:
        return await read
    except Exception:
        LOG.warning("A run card cost fact could not be read", fact=name, exc_info=True)
        return None


async def _accepted_tested_run(ctx: CopilotContext, saved: Workflow) -> WorkflowRun | None:
    if ctx.workflow_copilot_chat_id is None:
        return None
    chat = await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
        organization_id=ctx.organization_id, workflow_copilot_chat_id=ctx.workflow_copilot_chat_id
    )
    receipt = chat.accepted_tested_run if chat is not None else None
    if (
        receipt is None
        or receipt.workflow_run_id is None
        or (receipt.workflow_id, receipt.modified_at) != (saved.workflow_id, saved.modified_at)
    ):
        return None
    return await app.DATABASE.workflow_runs.get_workflow_run(
        receipt.workflow_run_id, organization_id=ctx.organization_id
    )


async def _based_on_run(ctx: CopilotContext, run_id: str | None, saved: Workflow | None) -> WorkflowRun | None:
    if run_id is not None:
        return await _fact(
            "based_on_run", app.DATABASE.workflow_runs.get_workflow_run(run_id, organization_id=ctx.organization_id)
        )
    if saved is None:
        return None
    tested, completed = await asyncio.gather(
        _fact("accepted_tested_run", _accepted_tested_run(ctx, saved)),
        _fact(
            "based_on_run",
            app.DATABASE.workflow_runs.get_latest_finished_run_for_workflow_version(
                ctx.organization_id, saved.workflow_permanent_id, saved.workflow_id, saved.modified_at
            ),
        ),
    )
    return max((run for run in (tested, completed) if run is not None), key=lambda run: run.created_at, default=None)


def _shown_run_inputs(
    ctx: CopilotContext, run_parameters: list[tuple[WorkflowParameter, WorkflowRunParameter]]
) -> dict[str, JsonValue]:
    inputs: dict[str, JsonValue] = {
        parameter.key: REDACTED_SECRET_PLACEHOLDER if is_sensitive_workflow_parameter(parameter) else value.value
        for parameter, value in run_parameters
    }
    return scrub_secrets_from_structure(ctx, inputs)


async def _run_cost_facts(
    ctx: CopilotContext,
    *,
    definition: WorkflowDefinition,
    based_on_run_id: str | None,
    saved: Workflow | None,
    runs_to_start: int,
) -> tuple[RunCostFacts, dict[str, JsonValue] | None]:
    usage, stop_rule, run = await asyncio.gather(
        _fact("usage", app.AGENT_FUNCTION.get_organization_usage_quota(organization_id=ctx.organization_id)),
        _fact("stop_rule", app.AGENT_FUNCTION.copilot_run_stop_rule(organization_id=ctx.organization_id)),
        _based_on_run(ctx, based_on_run_id, saved),
    )
    labels = _executable_workflow_block_labels(definition)
    based_on_inputs: dict[str, JsonValue] | None = None
    blocks_completed: int | None = None
    if run is not None:
        completed, run_parameters = await asyncio.gather(
            _fact(
                "run_blocks",
                app.DATABASE.observer.get_completed_workflow_run_block_labels(run.workflow_run_id, ctx.organization_id),
            ),
            _fact("run_inputs", app.DATABASE.workflow_runs.get_workflow_run_parameters(run.workflow_run_id)),
        )
        if run_parameters is not None:
            based_on_inputs = _shown_run_inputs(ctx, run_parameters)
        if completed is not None:
            blocks_completed = len(completed.intersection(labels))
    based_on_run_id = run.workflow_run_id if run is not None else None
    note = _QUOTA_UNREADABLE_NOTE if usage is None else usage.credits_note
    if usage is None or note is not None:
        return RunCostFacts(based_on_run_id=based_on_run_id, credits_note=note, stop_rule=stop_rule), based_on_inputs
    credits = _run_credits_used(run)
    whole_run = (
        run is not None
        and run.status == WorkflowRunStatus.completed
        and len(labels) > 0
        and blocks_completed == len(labels)
    )
    cost = RunCostFacts(
        runs_to_start=runs_to_start,
        based_on_run_id=based_on_run_id,
        based_on_credits=credits,
        blocks_completed=blocks_completed,
        blocks_total=len(labels) if run is not None else None,
        estimate_from_one_run=runs_to_start * credits if whole_run and credits is not None else None,
        balance_credits=usage.remaining_credits,
        stop_rule=stop_rule,
    )
    return cost, based_on_inputs


async def build_review(
    ctx: CopilotContext,
    *,
    credential_ids: list[str],
    credential_parameter_key: str,
    common_inputs: dict[str, JsonValue],
    action_summary: str,
) -> AccountGroupReview:
    await _require_run_role()
    if len(credential_ids) > ACCOUNT_GROUP_MAX_ITEMS:
        raise AccountGroupRefused(
            f"{len(credential_ids)} accounts were selected and one group runs at most "
            f"{ACCOUNT_GROUP_MAX_ITEMS}. Nothing was submitted. Offer the user separate reviewed groups. "
            f"All selected ids: {credential_ids}."
        )
    if not credential_ids:
        raise AccountGroupRefused("Select at least one account.")
    duplicates = sorted(credential_id for credential_id, count in Counter(credential_ids).items() if count > 1)
    if duplicates:
        raise AccountGroupRefused(f"These credential ids are selected more than once: {duplicates}.")
    if credential_parameter_key in common_inputs:
        raise AccountGroupRefused(f"{credential_parameter_key!r} is set per account, not as a common input.")
    _check_common_inputs(ctx, common_inputs)
    if await _chat_has_pending_proposal(ctx):
        raise AccountGroupRefused(_PENDING_PROPOSAL_REFUSAL)
    workflow = await _saved_workflow(ctx)
    await _check_connected_accounts(ctx, workflow, common_inputs)
    if reason := workflow_run_group_service.non_fresh_browser_reason(workflow):
        raise AccountGroupRefused(f"The saved workflow cannot run as a group because it has {reason}.")
    credential_parameter = await _require_credential_parameter(workflow, credential_parameter_key)
    credentials = await _resolve_credentials(ctx, credential_ids)
    try:
        await app.WORKFLOW_SERVICE.validate_schedule_parameters(
            workflow,
            await _organization(ctx),
            {**common_inputs, credential_parameter_key: credential_ids[0]},
        )
    except SkyvernHTTPException as e:
        raise AccountGroupRefused(str(e)) from e
    last_groups, prior_outcomes = await _latest_outcomes(ctx, workflow, credential_ids)
    rows = [
        AccountGroupRow(
            credential_id=credential_id,
            label=_label(credentials[credential_id]),
            identity=_identity_digest(credentials[credential_id]),
            prior_outcome=prior_outcomes.get(credential_id),
            last_group_id=last_groups.get(credential_id),
            preselected=prior_outcomes.get(credential_id) not in REPEAT_RISK_OUTCOMES,
        )
        for credential_id in credential_ids
    ]
    cost, based_on_inputs = await _run_cost_facts(
        ctx,
        definition=workflow.workflow_definition,
        based_on_run_id=None,
        saved=workflow,
        runs_to_start=sum(row.preselected for row in rows),
    )
    return AccountGroupReview(
        workflow_permanent_id=workflow.workflow_permanent_id,
        workflow_id=workflow.workflow_id,
        version=workflow.version,
        workflow_title=workflow.title,
        modified_at=workflow.modified_at,
        definition_hash=workflow_definition_hash(workflow),
        credential_parameter_key=credential_parameter_key,
        action_summary=redact_raw_secrets_for_prompt(scrub_secrets_from_text(ctx, action_summary)),
        common_inputs=common_inputs,
        clean_group_run_id=await _clean_group_run_id(ctx, workflow, credential_parameter, credential_ids),
        credential_parameter_used_by=_blocks_using_parameter(workflow, credential_parameter_key),
        rows=rows,
        cost=cost,
        based_on_inputs=based_on_inputs,
    )


async def _recheck_approval(ctx: CopilotContext, review: AccountGroupReview, approved_ids: list[str]) -> dict[str, str]:
    if not account_group_submit_enabled():
        raise AccountGroupRefused(
            "Account group submission was turned off after the review, so nothing was dispatched."
        )
    await _require_run_role()
    if await _chat_has_pending_proposal(ctx):
        raise AccountGroupRefused(_PENDING_PROPOSAL_REFUSAL)
    workflow = await _saved_workflow(ctx)
    if (workflow.workflow_id, workflow.version, workflow.modified_at) != (
        review.workflow_id,
        review.version,
        review.modified_at,
    ):
        raise AccountGroupRefused(
            "The saved workflow was updated after the review, so nothing was dispatched. "
            "Propose the group again so the user can review the current version."
        )
    _check_common_inputs(ctx, review.common_inputs)
    await _check_connected_accounts(ctx, workflow, review.common_inputs)
    await _require_credential_parameter(workflow, review.credential_parameter_key)
    credentials = await _resolve_credentials(ctx, approved_ids)
    shown = {row.credential_id: row.identity for row in review.rows}
    if renamed := sorted(i for i in approved_ids if _identity_digest(credentials[i]) != shown.get(i)):
        raise AccountGroupRefused(
            f"Nothing was dispatched: the saved account behind {renamed} changed after the review. "
            "Propose the group again so the user can review the current accounts."
        )
    reviewed = {row.credential_id: row.last_group_id for row in review.rows if row.credential_id in approved_ids}
    latest_groups = await app.DATABASE.workflow_run_groups.get_latest_group_ids_by_item_key(
        ctx.organization_id, workflow.workflow_permanent_id, _ACCOUNT_GROUP_KEY_ROOT, approved_ids
    )
    changed = sorted(
        credential_id for credential_id in approved_ids if latest_groups.get(credential_id) != reviewed[credential_id]
    )
    if changed:
        raise AccountGroupRefused(
            f"Nothing was dispatched: {changed} ran in another group after the review. "
            "Propose the group again so the user can review their latest outcomes."
        )
    return {credential_id: group_id for credential_id, group_id in reviewed.items() if group_id is not None}


def _rows(group: WorkflowRunGroupResponse, labels: dict[str, str]) -> list[AccountGroupRunRow]:
    return [
        AccountGroupRunRow(
            credential_id=item.key,
            label=labels.get(item.key, item.key),
            workflow_run_id=item.workflow_run_id,
            run_status=item.run_status,
            outcome=item.outcome,
        )
        for item in group.items
        if item.key in labels
    ]


async def submit_approved(
    ctx: CopilotContext, interaction: QuestionInteraction
) -> AccountGroupSubmission | AccountGroupRefusal:
    review = interaction.account_group_review
    decision = interaction.account_group_decision
    chat_id = ctx.workflow_copilot_chat_id
    if review is None or decision is None or not decision.approved or chat_id is None:
        return AccountGroupSubmission(
            approved=False,
            dispatched=False,
            cost=review.cost.not_started() if review is not None and review.cost is not None else None,
            note="Nothing was dispatched.",
        )
    approved_ids = decision.credential_ids
    # Held until the group exists, so a sibling tool call cannot publish a proposal after the recheck.
    async with ctx.proposal_mutation_lock:
        latest_groups = await _recheck_approval(ctx, review, approved_ids)
        organization = await _organization(ctx)
        request = _approved_request(chat_id, interaction.interaction_id, review, approved_ids)
        try:
            await PermissionCheckerFactory.get_instance().check(organization)
            group = await workflow_run_group_service.submit_workflow_run_group(
                organization,
                request,
                expected_workflow_modified_at=review.modified_at,
                expected_latest_groups=(_ACCOUNT_GROUP_KEY_ROOT, latest_groups),
            )
        except SkyvernHTTPException as e:
            return AccountGroupRefusal(status_code=e.status_code, error=str(e), dispatched=False)
        except HTTPException as e:
            return AccountGroupRefusal(status_code=e.status_code, error=str(e.detail), dispatched=False)
    try:
        await app.DATABASE.workflow_params.record_copilot_account_group_submission(
            ctx.organization_id, chat_id, interaction.interaction_id, group.workflow_run_group_id
        )
    except Exception:
        # Readers recover the link from the submission key, so the receipt still finds this group.
        LOG.warning(
            "Failed to record the account group on its review",
            workflow_run_group_id=group.workflow_run_group_id,
            exc_info=True,
        )
    submitted = interaction.model_copy(
        update={
            "account_group_review": review.model_copy(update={"workflow_run_group_id": group.workflow_run_group_id})
        }
    )
    if ctx.stream is not None:
        await ctx.stream.send(
            {"type": "question_resolved", "interaction": submitted.model_dump(mode="json"), "continued": True}
        )
    labels = {row.credential_id: row.label for row in review.rows}
    return AccountGroupSubmission(
        approved=True,
        dispatched=True,
        workflow_run_group_id=group.workflow_run_group_id,
        clean_group_run_id=review.clean_group_run_id,
        credential_parameter_used_by=review.credential_parameter_used_by,
        rows=_rows(group, labels),
        repeated_after_prior_effect=review.repeated_after_prior_effect(approved_ids),
        cost=review.cost.for_runs(len(approved_ids)) if review.cost is not None else None,
        note=_STATUS_NOTE,
    )


async def run_for_accounts(
    ctx: CopilotContext,
    *,
    tool_call_id: str,
    credential_ids: list[str],
    credential_parameter_key: str,
    common_inputs: dict[str, JsonValue],
    action_summary: str,
) -> AccountGroupSubmission | AccountGroupRefusal:
    try:
        review = await build_review(
            ctx,
            credential_ids=credential_ids,
            credential_parameter_key=credential_parameter_key,
            common_inputs=common_inputs,
            action_summary=action_summary,
        )
        recorded = await wait_for_interaction(
            ctx,
            QuestionInteraction(
                interaction_id=uuid4().hex,
                turn_id=ctx.turn_id,
                tool_call_id=tool_call_id,
                parts=[],
                account_group_review=review,
            ),
        )
        return await submit_approved(ctx, recorded)
    except AccountGroupRefused as e:
        return AccountGroupRefusal(error=str(e), dispatched=False)


async def _credential_parameters(workflow: Workflow) -> dict[str, WorkflowParameter]:
    parameters = await app.WORKFLOW_SERVICE.get_workflow_parameters(workflow_id=workflow.workflow_id)
    return {p.key: p for p in parameters if p.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID}


def _check_run_inputs(ctx: CopilotContext, inputs: dict[str, JsonValue]) -> None:
    if scrub_secrets_from_structure(ctx, inputs) != inputs:
        raise AccountGroupRefused(
            "An input holds a secret value, and secrets cannot be run inputs. Nothing was started. "
            "Use a saved credential for the secret instead."
        )
    _check_common_inputs(ctx, inputs)


def _single_run_credentials(parameters: list[WorkflowParameter], inputs: dict[str, JsonValue]) -> dict[str, str]:
    # Run starts a workflow whose credential_id input has no value and runs without that credential, so only inputs
    # that resolve to a credential id get a row. A null input takes the saved default, as the run service resolves it.
    return {
        parameter.key: value
        for parameter in parameters
        if parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID
        and isinstance(
            value := parameter.default_value if inputs.get(parameter.key) is None else inputs[parameter.key], str
        )
        and value.strip()
    }


async def _savable_draft(ctx: CopilotContext) -> tuple[Workflow, CopilotProposalMetadata] | None:
    """The chat's unsaved draft the card can save, or None when nothing is pending."""
    if not await _chat_has_pending_proposal(ctx):
        return None
    chat = (
        await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
            organization_id=ctx.organization_id, workflow_copilot_chat_id=ctx.workflow_copilot_chat_id
        )
        if ctx.workflow_copilot_chat_id
        else None
    )
    proposal = chat.proposed_workflow if chat is not None else None
    metadata = copilot_proposal_metadata(proposal)
    # An untokened proposal has nothing to bind the review to, and a settings-diverged one cannot vouch that its
    # stored bytes are what the user reviewed.
    if (
        not isinstance(proposal, dict)
        or metadata is None
        or "workflow_definition" not in proposal
        or metadata.claim_is_live(datetime.now(UTC))
        or ctx.settings_diverged_proposal_token == (metadata.owner_turn_id, metadata.revision)
    ):
        raise AccountGroupRefused(_SINGLE_RUN_PENDING_PROPOSAL_REFUSAL)
    try:
        return Workflow.model_validate(proposal), metadata
    except ValidationError as e:
        raise AccountGroupRefused(_SINGLE_RUN_PENDING_PROPOSAL_REFUSAL) from e


def _mentioned_input_defaults(value: object, defaults: dict[str, object]) -> dict[str, object]:
    """A login field can name an input bare or in a template, so the defaults of every input it mentions count."""
    # ponytail: a substring match can only over-count (an extra Accept), and covers any identifier the runtime accepts.
    text = json.dumps(value, ensure_ascii=False, default=str)
    return {key: defaults[key] for key in sorted(defaults) if key in text}


def _credential_bindings(workflow: Workflow) -> dict[str, list[str]]:
    """The logins and connected accounts this version runs with when no run input overrides them, resolved the
    way the runtime resolves them, since saving a draft keeps its definition and defaults for later runs."""
    definition = workflow.workflow_definition.model_dump(mode="json")
    parsed = {"workflow_definition": definition}
    defaults = {
        parameter["key"]: parameter.get("default_value")
        for parameter in declared_parameters(parsed)
        if isinstance(parameter, dict) and isinstance(parameter.get("key"), str)
    }
    # Mirrors resolve_credential_parameter_binding: a credential_id naming a parameter is that parameter's value.
    for parameter in declared_parameters(parsed):
        if (
            isinstance(parameter, dict)
            and str(parameter.get("parameter_type") or "").lower() == "credential"
            and isinstance(named := defaults.get(str(parameter.get("credential_id"))), str)
        ):
            parameter["credential_id"] = named
    parameter_ids = credential_param_ids(definition.get("parameters"))
    # Order is kept: the runtime tries the primary login first, then the fallbacks in order.
    bindings: dict[str, list[str]] = {}
    for parameter in definition.get("parameters") or []:
        if ids := _extract_credential_ids_from_workflow_parameters([parameter]):
            bindings[f"parameter:{parameter.get('key')}"] = ids
            bindings[f"fallbacks:{parameter.get('key')}"] = _extract_credential_ids_from_tool_value(
                parameter.get("fallback_credential_ids")
            )
    # Every parameter that is not a plain input, block output or context value can resolve a login or secret,
    # so it is compared whole; a new parameter type is covered without being listed here.
    for parameter in declared_parameters(parsed):
        if isinstance(parameter, dict) and parameter.get("parameter_type") not in _NON_LOGIN_PARAMETER_TYPES:
            content = {
                field: value
                for field, value in parameter.items()
                if field not in _PARAMETER_ROW_FIELDS and not field.endswith("_parameter_id")
            }
            content["input_defaults"] = _mentioned_input_defaults(content, defaults)
            bindings[f"login_parameter:{parameter.get('key')}"] = [json.dumps(content, sort_keys=True, default=str)]
    for block in workflow_blocks(parsed):
        if block_ids := {
            *_extract_credential_ids_from_workflow_parameters(block.get("parameters")),
            *_extract_credential_ids_from_tool_value(block.get("credential_id")),
            *block_credential_ids(block, parameter_ids),
        }:
            bindings[f"block:{block.get('label')}"] = sorted(block_ids)
        if refs := workflow_connection_refs(parsed, selected_labels={str(block.get("label"))}, run_parameters=defaults):
            bindings[f"connection:{block.get('label')}"] = sorted(refs)
    # The memory switches pick the managed profile over the credential's profile or a fresh browser.
    profile = [
        workflow.browser_profile_id or "",
        workflow.browser_profile_key or "",
        str(workflow.persist_browser_session),
        str(workflow.reuse_browser_session),
    ]
    bindings["browser_profile"] = [*profile, json.dumps(_mentioned_input_defaults(profile, defaults), default=str)]
    return bindings


async def build_single_run_review(
    ctx: CopilotContext, *, inputs: dict[str, JsonValue], action_summary: str
) -> AccountGroupReview:
    await _require_run_role()
    draft = await _savable_draft(ctx)
    metadata: CopilotProposalMetadata | None = None
    if draft is None:
        saved = await _saved_workflow(ctx)
        workflow = saved
        parameters = await app.WORKFLOW_SERVICE.get_workflow_parameters(workflow_id=workflow.workflow_id)
    else:
        workflow, metadata = draft
        stored = await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id=ctx.workflow_permanent_id, organization_id=ctx.organization_id
        )
        if stored is None:
            raise AccountGroupRefused(_SINGLE_RUN_PENDING_PROPOSAL_REFUSAL)
        saved = stored
        if not workflow.workflow_definition.blocks:
            raise AccountGroupRefused("The unsaved workflow change has no blocks to run, so nothing was started.")
        if _credential_bindings(workflow) != _credential_bindings(saved):
            raise AccountGroupRefused(_SINGLE_RUN_BINDING_REFUSAL)
        parameters = list(_declared_execution_snapshot(workflow).workflow_parameters)
    definition_credential_keys = {
        parameter.key
        for parameter in workflow.workflow_definition.parameters
        if isinstance(parameter, CredentialParameter)
    }
    if overridden := sorted(definition_credential_keys & set(inputs)):
        raise AccountGroupRefused(
            f"{overridden} are credentials set in the workflow itself, not inputs, so nothing was started. "
            "Propose the run again without them."
        )
    credential_inputs = _single_run_credentials(parameters, inputs)
    common_inputs = {key: value for key, value in inputs.items() if key not in credential_inputs}
    _check_run_inputs(ctx, common_inputs)
    if metadata is not None:
        await _check_draft_connected_accounts(ctx, workflow, common_inputs)
    credentials = await _resolve_credentials(ctx, list(dict.fromkeys(credential_inputs.values())))
    organization = await _organization(ctx)
    try:
        if metadata is None:
            await app.WORKFLOW_SERVICE.validate_schedule_parameters(
                workflow, organization, {**common_inputs, **credential_inputs}
            )
        else:
            # The saved rows describe the version the draft replaces, so the draft's inputs get the check the run
            # itself makes before it is created.
            await app.WORKFLOW_SERVICE._refuse_unusable_request_parameters(
                workflow=workflow,
                workflow_parameters=parameters,
                request_data={**common_inputs, **credential_inputs},
                organization=organization,
            )
    except SkyvernHTTPException as e:
        raise AccountGroupRefused(str(e)) from e
    # Same source as the pending proposal's Tested badge: the stored disposition, which may carry no run id.
    draft_tested = metadata is not None and metadata.disposition in ("review_tested", "auto_applicable")
    tested_workflow_run_id = metadata.workflow_run_id if metadata is not None and draft_tested else None
    cost, based_on_inputs = await _run_cost_facts(
        ctx,
        definition=workflow.workflow_definition,
        based_on_run_id=tested_workflow_run_id,
        saved=saved if metadata is None else None,
        runs_to_start=1,
    )
    return AccountGroupReview(
        workflow_permanent_id=saved.workflow_permanent_id,
        workflow_id=saved.workflow_id,
        version=saved.version,
        workflow_title=workflow.title,
        modified_at=saved.modified_at,
        definition_hash=workflow_definition_hash(saved),
        credential_parameter_key="",
        action_summary=redact_raw_secrets_for_prompt(scrub_secrets_from_text(ctx, action_summary)),
        common_inputs=common_inputs,
        single_run=True,
        credential_inputs=credential_inputs,
        saves_draft=metadata is not None,
        proposal_owner_turn_id=metadata.owner_turn_id if metadata is not None else None,
        proposal_revision=metadata.revision if metadata is not None else None,
        draft_tested=draft_tested,
        tested_workflow_run_id=tested_workflow_run_id,
        cost=cost,
        based_on_inputs=based_on_inputs,
        rows=[
            AccountGroupRow(
                credential_id=credential_id,
                label=_label(credentials[credential_id]),
                identity=_identity_digest(credentials[credential_id]),
                parameter_key=key,
            )
            for key, credential_id in credential_inputs.items()
        ],
    )


async def _recheck_single_run(ctx: CopilotContext, review: AccountGroupReview) -> None:
    if not single_run_submit_enabled():
        raise AccountGroupRefused("Starting runs from Copilot was turned off after the review, so nothing was started.")
    await _require_run_role()
    if await _chat_has_pending_proposal(ctx):
        raise AccountGroupRefused(_SINGLE_RUN_PENDING_PROPOSAL_REFUSAL)
    workflow = await _saved_workflow(ctx)
    if (workflow.workflow_id, workflow.version, workflow.modified_at) != (
        review.workflow_id,
        review.version,
        review.modified_at,
    ) or _single_run_credentials(
        list((await _credential_parameters(workflow)).values()), {**review.common_inputs, **review.credential_inputs}
    ) != review.credential_inputs:
        raise AccountGroupRefused(_SINGLE_RUN_STALE_VERSION_REFUSAL)
    await _recheck_single_run_inputs(ctx, review)


async def _recheck_single_run_inputs(ctx: CopilotContext, review: AccountGroupReview) -> None:
    _check_run_inputs(ctx, review.common_inputs)
    credentials = await _resolve_credentials(ctx, list(dict.fromkeys(row.credential_id for row in review.rows)))
    if changed := sorted(
        {row.credential_id for row in review.rows if _identity_digest(credentials[row.credential_id]) != row.identity}
    ):
        raise AccountGroupRefused(
            f"Nothing was started: the saved account behind {changed} changed after the review. "
            "Propose the run again so the user can review the current accounts."
        )


def _stored_review(chat: WorkflowCopilotChat, interaction_id: str) -> AccountGroupReview | None:
    for entry in chat.pending_turns.values():
        for item in entry.question_interactions:
            if item.interaction_id == interaction_id:
                return item.account_group_review
    return None


async def _accepted_while_waiting(
    ctx: CopilotContext, chat: WorkflowCopilotChat, interaction_id: str, review: AccountGroupReview
) -> Workflow:
    """The version an Accept saved from exactly the reviewed proposal, if nothing has written over it since."""
    stored = _stored_review(chat, interaction_id)
    saved = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=review.workflow_permanent_id, organization_id=ctx.organization_id
    )
    if (
        (stored is None or stored.accepted_workflow_id is None)
        and saved is not None
        and (saved.workflow_id, saved.version, saved.modified_at)
        == (review.workflow_id, review.version, review.modified_at)
    ):
        raise AccountGroupRefused(_SINGLE_RUN_DRAFT_REJECTED_REFUSAL)
    if (
        stored is None
        or stored.accepted_workflow_id is None
        or saved is None
        or (saved.workflow_id, saved.version, saved.modified_at)
        != (stored.accepted_workflow_id, stored.accepted_version, stored.accepted_modified_at)
    ):
        raise AccountGroupRefused(_SINGLE_RUN_STALE_VERSION_REFUSAL)
    await _check_connected_accounts(
        ctx, saved, review.common_inputs, action="run the workflow", blocked_tool=SINGLE_RUN_TOOL_NAME
    )
    return saved


async def _recheck_draft_save(
    ctx: CopilotContext, chat_id: str, interaction_id: str, review: AccountGroupReview
) -> WorkflowCopilotChat | Workflow:
    """The chat to save the reviewed draft from, or the version an Accept already saved from it."""
    if not single_run_submit_enabled():
        raise AccountGroupRefused("Starting runs from Copilot was turned off after the review, so nothing was started.")
    await _require_run_role()
    await _require_save_permission(chat_id)
    await _recheck_single_run_inputs(ctx, review)
    chat = await app.DATABASE.workflow_params.get_workflow_copilot_chat_by_id(
        organization_id=ctx.organization_id, workflow_copilot_chat_id=chat_id
    )
    if chat is not None and chat.proposed_workflow is None:
        return await _accepted_while_waiting(ctx, chat, interaction_id, review)
    saved = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=review.workflow_permanent_id, organization_id=ctx.organization_id
    )
    if saved is None or (saved.workflow_id, saved.version, saved.modified_at) != (
        review.workflow_id,
        review.version,
        review.modified_at,
    ):
        raise AccountGroupRefused(_SINGLE_RUN_STALE_VERSION_REFUSAL)
    metadata = copilot_proposal_metadata(chat.proposed_workflow if chat is not None else None)
    if (
        chat is None
        or metadata is None
        or (metadata.owner_turn_id, metadata.revision)
        != (
            review.proposal_owner_turn_id,
            review.proposal_revision,
        )
    ):
        raise AccountGroupRefused(_SINGLE_RUN_STALE_DRAFT_REFUSAL)
    if ctx.settings_diverged_proposal_token == (metadata.owner_turn_id, metadata.revision):
        raise AccountGroupRefused(_SINGLE_RUN_PENDING_PROPOSAL_REFUSAL)
    try:
        draft = Workflow.model_validate(chat.proposed_workflow)
    except ValidationError as e:
        raise AccountGroupRefused(_SINGLE_RUN_STALE_DRAFT_REFUSAL) from e
    await _check_draft_connected_accounts(ctx, draft, review.common_inputs)
    return chat


async def _start_run(
    organization: Organization,
    chat_id: str,
    interaction_id: str,
    review: AccountGroupReview,
    workflow_run_id: str,
    principal: RequestPrincipal | None,
) -> bool | None:
    """True once the run service returns; None when it raised but the run row exists or cannot be checked."""
    organization_id = organization.organization_id
    turn_context = skyvern_context.current()
    request_id = turn_context.request_id if turn_context else None
    # The turn's SkyvernContext carries copilot_session_id, which would make this run a Copilot test run.
    run_context = SkyvernContext(request_id=request_id, organization_id=organization_id)
    try:
        with skyvern_context.scoped(run_context), exclude_workflow_block_cost():
            async with organization_log_scope(organization_id), run_submission_slot(organization_id):
                # The slot wait can be long and no run row holds the version yet, so another chat's auto-accept may
                # have saved over it in place; check again right before the run is created.
                saved = await app.DATABASE.workflows.get_workflow_by_permanent_id(
                    workflow_permanent_id=review.workflow_permanent_id, organization_id=organization_id
                )
                if saved is None or (saved.workflow_id, saved.version, saved.modified_at) != (
                    review.workflow_id,
                    review.version,
                    review.modified_at,
                ):
                    raise AccountGroupRefused(_SINGLE_RUN_STALE_VERSION_REFUSAL)
                # Linked once the slot is held and before the run exists, so no cancel or crash can leave a run its
                # review does not point at.
                await app.DATABASE.workflow_params.record_copilot_single_run(
                    organization_id, chat_id, interaction_id, workflow_run_id
                )
                await workflow_service.run_workflow(
                    workflow_id=review.workflow_permanent_id,
                    organization=organization,
                    workflow_request=WorkflowRequestBody(data={**review.common_inputs, **review.credential_inputs}),
                    version=review.version,
                    request_id=request_id,
                    trigger_type=WorkflowRunTriggerType.manual,
                    created_by=trigger_owner_id(principal, organization_id),
                    refuse_unusable_parameters_before_create=True,
                    workflow_run_id=workflow_run_id,
                )
    except AccountGroupRefused:
        raise
    except Exception:
        LOG.warning("Starting a saved workflow run from Copilot raised", workflow_run_id=workflow_run_id, exc_info=True)
        try:
            run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id, organization_id=organization_id)
        except Exception:
            LOG.warning("Could not check whether the run exists", workflow_run_id=workflow_run_id, exc_info=True)
            return None
        if run is not None:
            return None
        await app.DATABASE.workflow_params.unlink_copilot_single_run(
            organization_id, chat_id, interaction_id, workflow_run_id
        )
        raise
    LOG.info(
        "Copilot started a saved workflow run",
        chat_id=chat_id,
        interaction_id=interaction_id,
        workflow_run_id=workflow_run_id,
    )
    try:
        await app.AGENT_FUNCTION.on_run_created(
            organization_id=organization_id,
            run_id=workflow_run_id,
            run_type=RunType.workflow_run,
            caller_type=CallerType.USER,
        )
    except Exception:
        LOG.warning(
            "on_run_created failed for a Copilot saved workflow run", workflow_run_id=workflow_run_id, exc_info=True
        )
    return True


async def _recheck_and_start_run(
    ctx: CopilotContext, chat_id: str, interaction_id: str, review: AccountGroupReview, workflow_run_id: str
) -> bool | None | AccountGroupRefusal:
    # Held through dispatch, so a sibling tool call cannot publish a proposal between the recheck and the run.
    async with ctx.proposal_mutation_lock:
        await _recheck_single_run(ctx, review)
        organization = await _organization(ctx)
        try:
            await PermissionCheckerFactory.get_instance().check(organization)
            await app.RATE_LIMITER.rate_limit_submit_run(organization.organization_id)
            return await _start_run(
                organization, chat_id, interaction_id, review, workflow_run_id, get_request_principal()
            )
        except MissingBrowserAddressError as e:
            return AccountGroupRefusal(status_code=400, error=str(e), dispatched=False)
        except SkyvernHTTPException as e:
            return AccountGroupRefusal(status_code=e.status_code, error=str(e), dispatched=False)
        except HTTPException as e:
            return AccountGroupRefusal(status_code=e.status_code, error=str(e.detail), dispatched=False)


def _point_ctx_at_saved(ctx: CopilotContext, saved: Workflow) -> None:
    """Drop the consumed proposal token and draft so a later edit this turn publishes against the saved version."""
    ctx.proposal_owner_turn_id = None
    ctx.proposal_revision = None
    ctx.proposal_canonical_fingerprint = None
    ctx.proposal_workflow_run_id = None
    ctx.has_staged_proposal = False
    ctx.staged_workflow = None
    ctx.staged_workflow_yaml = None
    ctx.last_workflow = None
    ctx.last_workflow_yaml = None
    ctx.last_good_workflow = None
    ctx.last_good_workflow_yaml = None
    ctx.workflow_id = saved.workflow_id
    ctx.workflow_yaml = workflow_to_copilot_yaml(saved)
    ctx.authored_private_workflow_settings = {}
    ctx.private_workflow_settings = {}


async def _start_saved_draft_run(
    ctx: CopilotContext,
    organization: Organization,
    chat_id: str,
    interaction_id: str,
    review: AccountGroupReview,
    saved: Workflow,
    changed_fields: tuple[str, ...],
    workflow_run_id: str,
    *,
    accepted_while_waiting: bool = False,
) -> tuple[bool | None, str | None]:
    """Whether the run started (None: may or may not exist), and why not; never raises, since the version exists."""
    ctx.run_card_saved_workflow = saved
    if await adopt_stored_workflow_candidate(ctx) != "cleared":
        _point_ctx_at_saved(ctx, saved)
    try:
        if changed_fields:
            await app.AGENT_FUNCTION.record_audit_event(
                get_request_principal(),
                AuditEvent(
                    organization_id=organization.organization_id,
                    action="workflow.update",
                    resource_type="workflow",
                    resource_id=saved.workflow_permanent_id,
                    changed_fields=changed_fields,
                ),
            )
        await app.DATABASE.workflow_params.record_copilot_single_run_save(
            organization.organization_id,
            chat_id,
            interaction_id,
            saved_workflow_id=saved.workflow_id,
            saved_version=saved.version,
            rollback_workflow=saved.model_dump(mode="json"),
        )
        current = await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id=saved.workflow_permanent_id, organization_id=organization.organization_id
        )
    except Exception:
        LOG.warning("Copilot saved a draft from the run card but could not record it", exc_info=True)
        return False, "the save could not be recorded in this chat. Propose the run again to run the saved version."
    # Another chat can rewrite this version in place under the same id, so the content must still be the saved draft.
    if (
        current is None
        or current.workflow_id != saved.workflow_id
        or workflow_definition_hash(current) != workflow_definition_hash(saved)
    ):
        return False, _SINGLE_RUN_STALE_VERSION_REFUSAL
    if accepted_while_waiting:
        current = saved
    run_review = review.model_copy(
        update={"workflow_id": current.workflow_id, "version": current.version, "modified_at": current.modified_at}
    )
    try:
        await PermissionCheckerFactory.get_instance().check(organization)
        await app.RATE_LIMITER.rate_limit_submit_run(organization.organization_id)
        started = await _start_run(
            organization, chat_id, interaction_id, run_review, workflow_run_id, get_request_principal()
        )
    except AccountGroupRefused as e:
        return False, str(e)
    except (MissingBrowserAddressError, SkyvernHTTPException) as e:
        return False, str(e)
    except HTTPException as e:
        return False, str(e.detail)
    except Exception:
        # _start_run returns None when the run may exist and raises only once it confirmed the run does not.
        LOG.warning("Starting the run of a saved Copilot draft raised", workflow_run_id=workflow_run_id, exc_info=True)
        return False, "the run could not be started, so no run exists. Propose the run again to run the saved version."
    if started:
        try:
            await app.DATABASE.workflow_params.record_copilot_single_run_save(
                organization.organization_id,
                chat_id,
                interaction_id,
                saved_workflow_id=saved.workflow_id,
                saved_version=saved.version,
                run_started=True,
            )
        except Exception:
            LOG.warning("Failed to record that a saved Copilot draft's run started", exc_info=True)
    return started, None


async def _save_and_start_run(
    ctx: CopilotContext,
    chat_id: str,
    interaction: QuestionInteraction,
    review: AccountGroupReview,
    workflow_run_id: str,
) -> SingleRunSubmission | AccountGroupRefusal:
    # Held through dispatch, so a sibling tool call cannot publish or adopt a proposal between the recheck and the run.
    async with ctx.proposal_mutation_lock:
        checked = await _recheck_draft_save(ctx, chat_id, interaction.interaction_id, review)
        organization = await _organization(ctx)
        accepted_while_waiting = isinstance(checked, Workflow)
        changed_fields: tuple[str, ...] = ()
        if isinstance(checked, Workflow):
            saved = checked
        else:
            try:
                saved, changed_fields = await accept_copilot_proposal(
                    organization,
                    checked,
                    owner_turn_id=review.proposal_owner_turn_id,
                    revision=review.proposal_revision,
                    auto_accept=None,
                )
            except HTTPException as e:
                # A 409 can mean the user's own Accept of this draft won the claim, so it must not deny a save.
                prefix = "Nothing was started" if e.status_code == 409 else "Nothing was saved or started"
                return AccountGroupRefusal(status_code=e.status_code, error=f"{prefix}: {e.detail}", dispatched=False)
        started, failure = await _start_saved_draft_run(
            ctx,
            organization,
            chat_id,
            interaction.interaction_id,
            review,
            saved,
            changed_fields,
            workflow_run_id,
            accepted_while_waiting=accepted_while_waiting,
        )
    resolved = review.model_copy(
        update={
            "saved_workflow_id": saved.workflow_id,
            "saved_version": saved.version,
            "workflow_run_id": None if started is False else workflow_run_id,
            "run_started": True if started else None,
        }
    )
    if ctx.stream is not None:
        try:
            await ctx.stream.send(
                {
                    "type": "question_resolved",
                    "interaction": interaction.model_copy(update={"account_group_review": resolved}).model_dump(
                        mode="json"
                    ),
                    "continued": True,
                }
            )
        except Exception:
            LOG.warning("Failed to send the saved draft's run card update", exc_info=True)
    saved_note = (
        f"The user accepted the draft as version {saved.version} while the card waited, so the run uses that version."
        if accepted_while_waiting
        else f"The draft was saved as version {saved.version}."
    )
    if started is False:
        return SingleRunSubmission(
            ok=False,
            approved=True,
            dispatched=False,
            saved_version=saved.version,
            error=f"{saved_note} The run was not started: {failure}",
            cost=review.cost.not_started() if review.cost is not None else None,
        )
    return SingleRunSubmission(
        ok=started is True,
        approved=True,
        dispatched=started,
        workflow_run_id=workflow_run_id,
        saved_version=saved.version,
        error=None if started else f"{saved_note} {_SINGLE_RUN_UNCONFIRMED}",
        note=f"{saved_note} {_SINGLE_RUN_NOTE}" if started else None,
        cost=review.cost,
    )


_DispatchResult = bool | None | AccountGroupRefusal | SingleRunSubmission

_PENDING_DISPATCHES: set[asyncio.Task[_DispatchResult]] = set()


def _release_dispatch(task: asyncio.Task[_DispatchResult]) -> None:
    _PENDING_DISPATCHES.discard(task)
    if not task.cancelled():
        task.exception()


async def submit_single_run(
    ctx: CopilotContext, interaction: QuestionInteraction
) -> SingleRunSubmission | AccountGroupRefusal:
    review = interaction.account_group_review
    decision = interaction.account_group_decision
    chat_id = ctx.workflow_copilot_chat_id
    if review is None or decision is None or not decision.approved or chat_id is None:
        return SingleRunSubmission(
            approved=False,
            dispatched=False,
            note="Nothing was started.",
            cost=review.cost.not_started() if review is not None and review.cost is not None else None,
        )
    workflow_run_id = generate_workflow_run_id()
    # Shielded so cancelling the tool call cannot cut dispatch, or the lock it holds, short; the set keeps the
    # task alive after a cancel, since the event loop holds only a weak reference.
    dispatch: asyncio.Task[_DispatchResult] = asyncio.create_task(
        _save_and_start_run(ctx, chat_id, interaction, review, workflow_run_id)
        if review.saves_draft
        else _recheck_and_start_run(ctx, chat_id, interaction.interaction_id, review, workflow_run_id)
    )
    _PENDING_DISPATCHES.add(dispatch)
    dispatch.add_done_callback(_release_dispatch)
    dispatched = await asyncio.shield(dispatch)
    if isinstance(dispatched, (AccountGroupRefusal, SingleRunSubmission)):
        return dispatched
    submitted = interaction.model_copy(
        update={"account_group_review": review.model_copy(update={"workflow_run_id": workflow_run_id})}
    )
    if ctx.stream is not None:
        await ctx.stream.send(
            {"type": "question_resolved", "interaction": submitted.model_dump(mode="json"), "continued": True}
        )
    return SingleRunSubmission(
        ok=dispatched is True,
        approved=decision.approved,
        dispatched=dispatched,
        workflow_run_id=workflow_run_id,
        error=None if dispatched else _SINGLE_RUN_UNCONFIRMED,
        note=_SINGLE_RUN_NOTE if dispatched else None,
        cost=review.cost,
    )


async def run_saved_workflow(
    ctx: CopilotContext, *, tool_call_id: str, inputs: dict[str, JsonValue], action_summary: str
) -> SingleRunSubmission | AccountGroupRefusal:
    try:
        review = await build_single_run_review(ctx, inputs=inputs, action_summary=action_summary)
        recorded = await wait_for_interaction(
            ctx,
            QuestionInteraction(
                interaction_id=uuid4().hex,
                turn_id=ctx.turn_id,
                tool_call_id=tool_call_id,
                parts=[],
                account_group_review=review,
            ),
        )
        return await submit_single_run(ctx, recorded)
    except AccountGroupRefused as e:
        return AccountGroupRefusal(error=str(e), dispatched=False)


async def _group_for_this_workflow(ctx: CopilotContext, workflow_run_group_id: str) -> WorkflowRunGroupResponse:
    try:
        group = await workflow_run_group_service.get_workflow_run_group(workflow_run_group_id, ctx.organization_id)
    except SkyvernHTTPException as e:
        raise AccountGroupRefused(str(e)) from e
    if group.workflow_permanent_id != ctx.workflow_permanent_id:
        raise AccountGroupRefused(f"Group {workflow_run_group_id} runs a different workflow.")
    return group


async def _labels(ctx: CopilotContext, group: WorkflowRunGroupResponse) -> dict[str, str]:
    credentials = await load_credentials(
        ctx.organization_id, credential_ids=[item.key for item in group.items], include_deleted=True
    )
    return {credential.credential_id: _label(credential) for credential in credentials}


async def _group_state(
    ctx: CopilotContext, group: WorkflowRunGroupResponse, *, cancel_approved: bool | None = None
) -> AccountGroupState:
    return AccountGroupState(
        workflow_run_group_id=group.workflow_run_group_id,
        status=group.status,
        rows=_rows(group, await _labels(ctx, group)),
        cancel_approved=cancel_approved,
    )


async def account_group_status(
    ctx: CopilotContext, workflow_run_group_id: str
) -> AccountGroupState | AccountGroupRefusal:
    try:
        group = await _group_for_this_workflow(ctx, workflow_run_group_id)
    except AccountGroupRefused as e:
        return AccountGroupRefusal(error=str(e))
    return await _group_state(ctx, group)


async def cancel_account_group(
    ctx: CopilotContext, *, tool_call_id: str, workflow_run_group_id: str
) -> AccountGroupState | AccountGroupRefusal:
    chat_id = ctx.workflow_copilot_chat_id
    try:
        group = await _group_for_this_workflow(ctx, workflow_run_group_id)
        if chat_id is None or not group.submission_key.startswith(account_group_key_prefix(chat_id)):
            raise AccountGroupRefused(
                f"Group {workflow_run_group_id} was not started from this chat, so it cannot be canceled here."
            )
        state = await _group_state(ctx, group)
        unfinished = [row for row in state.rows if row.outcome in _UNFINISHED_OUTCOMES]
        if group.status != WorkflowRunGroupStatus.active or not unfinished:
            raise AccountGroupRefused(f"Group {workflow_run_group_id} has no runs left to cancel.")
        recorded = await wait_for_interaction(
            ctx,
            QuestionInteraction(
                interaction_id=uuid4().hex,
                turn_id=ctx.turn_id,
                tool_call_id=tool_call_id,
                parts=[],
                account_group_cancel=AccountGroupCancelReview(
                    workflow_run_group_id=workflow_run_group_id, unfinished_rows=unfinished
                ),
            ),
        )
        decision = recorded.account_group_decision
        if decision is None or not decision.approved:
            group = await _group_for_this_workflow(ctx, workflow_run_group_id)
            return await _group_state(ctx, group, cancel_approved=False)
        group = await workflow_run_group_service.cancel_workflow_run_group(workflow_run_group_id, ctx.organization_id)
    except AccountGroupRefused as e:
        return AccountGroupRefusal(error=str(e))
    except SkyvernHTTPException as e:
        return AccountGroupRefusal(status_code=e.status_code, error=str(e))
    return await _group_state(ctx, group, cancel_approved=True)
