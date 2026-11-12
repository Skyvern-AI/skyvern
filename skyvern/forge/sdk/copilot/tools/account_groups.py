from __future__ import annotations

import asyncio
import hmac
import json
from collections import Counter
from collections.abc import Iterable
from hashlib import sha256
from typing import Literal
from uuid import uuid4

import structlog
from fastapi import HTTPException
from pydantic import BaseModel, Field, JsonValue

from skyvern.config import settings
from skyvern.exceptions import MissingBrowserAddressError, SkyvernHTTPException
from skyvern.forge import app
from skyvern.forge.sdk.copilot.ask_user import (
    REPEAT_RISK_OUTCOMES,
    AccountGroupCancelReview,
    AccountGroupReview,
    AccountGroupRow,
    AccountGroupRunRow,
    QuestionInteraction,
    wait_for_interaction,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.cost_telemetry import exclude_workflow_block_cost
from skyvern.forge.sdk.copilot.credential_resolution import load_credentials
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure, scrub_secrets_from_text
from skyvern.forge.sdk.copilot.tools.credentials import (
    _credential_run_approval_error,
    _extract_credential_ids_from_tool_value,
    run_credential_gate,
)
from skyvern.forge.sdk.copilot.tools.mcp_hooks import _chat_has_pending_proposal
from skyvern.forge.sdk.copilot.workflow_credential_utils import parse_workflow_yaml, workflow_blocks
from skyvern.forge.sdk.copilot.workflow_yaml import workflow_to_copilot_yaml
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.permissions.permission_checker_factory import PermissionCheckerFactory
from skyvern.forge.sdk.core.run_submission_gate import run_submission_slot
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.db.id import generate_workflow_run_id
from skyvern.forge.sdk.schemas.credentials import Credential
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services.request_principal import (
    RequestPrincipal,
    enforce_principal_role,
    get_request_principal,
    trigger_owner_id,
)
from skyvern.forge.sdk.workflow.block_notes import strip_inert_block_fields
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter, WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.tags import CallerType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRequestBody
from skyvern.schemas.run_enums import RunType
from skyvern.schemas.workflow_run_groups import (
    MAX_WORKFLOW_RUN_GROUP_ITEMS,
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
    "their saved defaults; a credential_id input with neither runs without a credential, as Run does. The result "
    "gives the workflow_run_id; get_run_results with that id reads the run's progress and outcome."
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

_DEFINITION_HASH_DOMAIN = b"skyvern.copilot_account_group_definition.v1\0"
_IDENTITY_HASH_DOMAIN = b"skyvern.copilot_account_group_identity.v1\0"
_ACCOUNT_GROUP_KEY_ROOT = "copilot:"

_PENDING_PROPOSAL_REFUSAL = (
    "Nothing was submitted: this chat has a workflow change that is not saved yet, and a group runs the "
    "saved version. End this turn so the change can be saved (the user accepts it, or auto-accept applies "
    "it when the turn ends) or rejected, then propose the group again."
)

_SINGLE_RUN_PENDING_PROPOSAL_REFUSAL = (
    "Nothing was started: this chat has a workflow change waiting to be accepted or rejected, and this tool runs "
    "the saved version."
)

_SINGLE_RUN_STALE_VERSION_REFUSAL = "The saved workflow was updated after the review, so nothing was started."

_SINGLE_RUN_NOTE = "The run is not tied to this turn and keeps going after it ends."

_SINGLE_RUN_UNCONFIRMED = (
    "Starting the run raised an error after its run id was created, so the run may or may not exist."
)

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
    note: str


class SingleRunSubmission(BaseModel):
    ok: bool = True
    approved: bool
    dispatched: bool | None
    workflow_run_id: str | None = None
    error: str | None = None
    note: str | None = None


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
    ctx: CopilotContext, workflow: Workflow, common_inputs: dict[str, JsonValue]
) -> None:
    gate = await run_credential_gate(
        ctx,
        workflow.workflow_definition,
        labels=None,
        run_parameters=dict(common_inputs),
        action="run the workflow for these accounts",
        blocked_tool="run_workflow_for_accounts",
    )
    if gate.error is not None:
        raise AccountGroupRefused(gate.error)


async def build_review(
    ctx: CopilotContext,
    *,
    credential_ids: list[str],
    credential_parameter_key: str,
    common_inputs: dict[str, JsonValue],
    action_summary: str,
) -> AccountGroupReview:
    await _require_run_role()
    if len(credential_ids) > MAX_WORKFLOW_RUN_GROUP_ITEMS:
        raise AccountGroupRefused(
            f"{len(credential_ids)} accounts were selected and one group runs at most "
            f"{MAX_WORKFLOW_RUN_GROUP_ITEMS}. Nothing was submitted. Offer the user separate reviewed groups. "
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
        rows=[
            AccountGroupRow(
                credential_id=credential_id,
                label=_label(credentials[credential_id]),
                identity=_identity_digest(credentials[credential_id]),
                prior_outcome=prior_outcomes.get(credential_id),
                last_group_id=last_groups.get(credential_id),
                preselected=prior_outcomes.get(credential_id) not in REPEAT_RISK_OUTCOMES,
            )
            for credential_id in credential_ids
        ],
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
        return AccountGroupSubmission(approved=False, dispatched=False, note="Nothing was dispatched.")
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


async def build_single_run_review(
    ctx: CopilotContext, *, inputs: dict[str, JsonValue], action_summary: str
) -> AccountGroupReview:
    await _require_run_role()
    if await _chat_has_pending_proposal(ctx):
        raise AccountGroupRefused(_SINGLE_RUN_PENDING_PROPOSAL_REFUSAL)
    workflow = await _saved_workflow(ctx)
    parameters = await app.WORKFLOW_SERVICE.get_workflow_parameters(workflow_id=workflow.workflow_id)
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
    credentials = await _resolve_credentials(ctx, list(dict.fromkeys(credential_inputs.values())))
    try:
        await app.WORKFLOW_SERVICE.validate_schedule_parameters(
            workflow, await _organization(ctx), {**common_inputs, **credential_inputs}
        )
    except SkyvernHTTPException as e:
        raise AccountGroupRefused(str(e)) from e
    return AccountGroupReview(
        workflow_permanent_id=workflow.workflow_permanent_id,
        workflow_id=workflow.workflow_id,
        version=workflow.version,
        workflow_title=workflow.title,
        modified_at=workflow.modified_at,
        definition_hash=workflow_definition_hash(workflow),
        credential_parameter_key="",
        action_summary=redact_raw_secrets_for_prompt(scrub_secrets_from_text(ctx, action_summary)),
        common_inputs=common_inputs,
        single_run=True,
        credential_inputs=credential_inputs,
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
    _check_run_inputs(ctx, review.common_inputs)
    credentials = await _resolve_credentials(ctx, list(dict.fromkeys(row.credential_id for row in review.rows)))
    if changed := sorted(
        {row.credential_id for row in review.rows if _identity_digest(credentials[row.credential_id]) != row.identity}
    ):
        raise AccountGroupRefused(
            f"Nothing was started: the saved account behind {changed} changed after the review. "
            "Propose the run again so the user can review the current accounts."
        )


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


_PENDING_DISPATCHES: set[asyncio.Task[bool | None | AccountGroupRefusal]] = set()


def _release_dispatch(task: asyncio.Task[bool | None | AccountGroupRefusal]) -> None:
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
        return SingleRunSubmission(approved=False, dispatched=False, note="Nothing was started.")
    workflow_run_id = generate_workflow_run_id()
    # Shielded so cancelling the tool call cannot cut dispatch, or the lock it holds, short; the set keeps the
    # task alive after a cancel, since the event loop holds only a weak reference.
    dispatch = asyncio.create_task(
        _recheck_and_start_run(ctx, chat_id, interaction.interaction_id, review, workflow_run_id)
    )
    _PENDING_DISPATCHES.add(dispatch)
    dispatch.add_done_callback(_release_dispatch)
    dispatched = await asyncio.shield(dispatch)
    if isinstance(dispatched, AccountGroupRefusal):
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
