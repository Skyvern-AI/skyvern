from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from skyvern.forge import app
from skyvern.forge.sdk.copilot.human_input_wait import pause_human_input
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_text
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.workflow_run_groups import WorkflowRunGroupItemOutcome
from skyvern.utils.contained_effects import contained_effect

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import CopilotContext

LOG = structlog.get_logger()


class QuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    choices: list[str] = Field(default_factory=list)


class AskUserArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parts: list[QuestionInput] = Field(min_length=1)


class QuestionChoice(BaseModel):
    choice_id: str
    text: str


class QuestionPart(BaseModel):
    part_id: str
    prompt: str
    choices: list[QuestionChoice] = Field(default_factory=list)


class QuestionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_id: str
    choice_id: str | None = None
    text: str | None = None


ACCOUNT_GROUP_SUBMIT_TOOL_NAME = "run_workflow_for_accounts"
ACCOUNT_GROUP_STATUS_TOOL_NAME = "get_account_group_status"
ACCOUNT_GROUP_CANCEL_TOOL_NAME = "cancel_account_group"

REPEAT_RISK_OUTCOMES = (
    WorkflowRunGroupItemOutcome.completed,
    WorkflowRunGroupItemOutcome.unknown,
    WorkflowRunGroupItemOutcome.pending,
    WorkflowRunGroupItemOutcome.in_progress,
)


class AccountGroupDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    credential_ids: list[str] = Field(default_factory=list)


class AccountGroupRow(BaseModel):
    credential_id: str
    label: str
    identity: str = ""
    prior_outcome: WorkflowRunGroupItemOutcome | None = None
    last_group_id: str | None = None
    preselected: bool = True


class AccountGroupReview(BaseModel):
    workflow_permanent_id: str
    workflow_id: str
    version: int
    workflow_title: str
    modified_at: datetime
    definition_hash: str
    credential_parameter_key: str
    action_summary: str
    common_inputs: dict[str, JsonValue] = Field(default_factory=dict)
    clean_group_run_id: str | None = None
    credential_parameter_used_by: list[str] = Field(default_factory=list)
    rows: list[AccountGroupRow]
    workflow_run_group_id: str | None = None
    # Stored here rather than on QuestionResponse, whose older versions forbid unknown fields.
    decision: AccountGroupDecision | None = Field(default=None, exclude_if=lambda decision: decision is None)

    def repeated_after_prior_effect(self, approved_ids: list[str]) -> list[AccountGroupRow]:
        return [
            row for row in self.rows if row.credential_id in approved_ids and row.prior_outcome in REPEAT_RISK_OUTCOMES
        ]


class AccountGroupRunRow(BaseModel):
    credential_id: str
    label: str
    workflow_run_id: str
    run_status: WorkflowRunStatus | None
    outcome: WorkflowRunGroupItemOutcome


class AccountGroupCancelReview(BaseModel):
    workflow_run_group_id: str
    unfinished_rows: list[AccountGroupRunRow]
    decision: AccountGroupDecision | None = Field(default=None, exclude_if=lambda decision: decision is None)


class QuestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[QuestionAnswer] = Field(default_factory=list)
    text: str | None = None
    skipped: bool = False
    # Server-owned result of screening the free-text answer. It is persisted
    # with the interaction so active and reconstructed policies enforce the
    # same trust floor without retaining the detected literal.
    raw_secret_detected: bool = Field(default=False, exclude_if=lambda detected: not detected)


class QuestionInteraction(BaseModel):
    interaction_id: str
    turn_id: str
    tool_call_id: str
    parts: list[QuestionPart]
    status: Literal["pending", "resolved", "cancelled", "interrupted"] = "pending"
    response: QuestionResponse | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
    account_group_review: AccountGroupReview | None = Field(default=None, exclude_if=lambda review: review is None)
    account_group_cancel: AccountGroupCancelReview | None = Field(
        default=None, exclude_if=lambda review: review is None
    )

    @property
    def account_group_decision(self) -> AccountGroupDecision | None:
        if self.account_group_review is not None:
            return self.account_group_review.decision
        return self.account_group_cancel.decision if self.account_group_cancel is not None else None

    def tool_result(self) -> dict[str, Any]:
        if self.status != "resolved" or self.response is None:
            raise ValueError("Question has no accepted response")
        if self.account_group_review is not None:
            decision = self.account_group_decision
            approved_ids = decision.credential_ids if decision is not None and decision.approved else []
            group_id = self.account_group_review.workflow_run_group_id
            # None: approved, but no group is linked, so whether it was submitted is unknown.
            dispatched = True if group_id is not None else (None if approved_ids else False)
            return {
                "ok": True,
                "interaction_id": self.interaction_id,
                "tool_call_id": self.tool_call_id,
                "approved": bool(approved_ids),
                "approved_credential_ids": approved_ids,
                "repeated_after_prior_effect": [
                    row.model_dump(mode="json", exclude={"identity"})
                    for row in self.account_group_review.repeated_after_prior_effect(approved_ids)
                ],
                "dispatched": dispatched,
                "workflow_run_group_id": group_id,
            }
        if self.account_group_cancel is not None:
            decision = self.account_group_decision
            return {
                "ok": True,
                "interaction_id": self.interaction_id,
                "tool_call_id": self.tool_call_id,
                "workflow_run_group_id": self.account_group_cancel.workflow_run_group_id,
                "cancel_approved": decision is not None and decision.approved,
            }
        answers = {answer.part_id: answer for answer in self.response.answers}
        parts = []
        for part in self.parts:
            answer = answers.get(part.part_id)
            choice = next(
                (choice for choice in part.choices if answer is not None and choice.choice_id == answer.choice_id),
                None,
            )
            parts.append(
                {
                    "part_id": part.part_id,
                    "prompt": part.prompt,
                    "status": "answered" if answer is not None else "unanswered",
                    "choice": choice.model_dump() if choice is not None else None,
                    "text": answer.text if answer is not None else None,
                }
            )
        return {
            "ok": True,
            "interaction_id": self.interaction_id,
            "tool_call_id": self.tool_call_id,
            "skipped": self.response.skipped,
            "text": self.response.text,
            "parts": parts,
        }


QUESTION_HEARTBEAT_GRACE = timedelta(seconds=30)
QUESTION_CLIENT_GRACE = timedelta(minutes=5)
QUESTION_POLL_SECONDS = 1.0
QUESTION_CLEANUP_TIMEOUT_SECONDS = 5.0
_PENDING_QUESTION_CLEANUPS: set[asyncio.Task[QuestionInteraction | None]] = set()


def _release_question_cleanup(task: asyncio.Task[QuestionInteraction | None]) -> None:
    _PENDING_QUESTION_CLEANUPS.discard(task)
    if not task.cancelled():
        task.exception()  # Database failures are logged by the repository.


def question_wait_is_live(heartbeat_at: datetime | None, now: datetime) -> bool:
    return heartbeat_at is not None and now - heartbeat_at < QUESTION_HEARTBEAT_GRACE


def create_question_interaction(
    arguments: AskUserArguments,
    *,
    turn_id: str,
    tool_call_id: str,
    ctx: CopilotContext | None = None,
) -> QuestionInteraction:
    def safe_text(text: str) -> str:
        if ctx is not None:
            text = scrub_secrets_from_text(ctx, text)
        return redact_raw_secrets_for_prompt(text)

    return QuestionInteraction(
        interaction_id=uuid4().hex,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        parts=[
            QuestionPart(
                part_id=uuid4().hex,
                prompt=safe_text(part.prompt),
                choices=[QuestionChoice(choice_id=uuid4().hex, text=safe_text(choice)) for choice in part.choices],
            )
            for part in arguments.parts
        ],
    )


def _validate_account_group_decision(
    interaction: QuestionInteraction, response: QuestionResponse, decision: AccountGroupDecision | None
) -> None:
    review = interaction.account_group_review
    if interaction.account_group_cancel is not None:
        if response.answers or response.text is not None:
            raise ValueError("A cancel review takes only a decision")
        if decision is None and not response.skipped:
            raise ValueError("A cancel review requires a decision")
        if decision is not None and decision.credential_ids:
            raise ValueError("A cancel review stops the whole group, not chosen accounts")
        return
    if review is None:
        if decision is not None:
            raise ValueError("This question has no account review")
        return
    if response.answers or response.text is not None:
        raise ValueError("An account review takes only a decision")
    if decision is None:
        if not response.skipped:
            raise ValueError("An account review requires a decision")
        return
    if not decision.approved:
        if decision.credential_ids:
            raise ValueError("A declined review approves no accounts")
        return
    if not decision.credential_ids:
        raise ValueError("Approve at least one account")
    if len(set(decision.credential_ids)) != len(decision.credential_ids):
        raise ValueError("An approval lists an account twice")
    if not set(decision.credential_ids) <= {row.credential_id for row in review.rows}:
        raise ValueError("An approval lists an account the review did not")


def resolve_question_response(
    interaction: QuestionInteraction,
    response: QuestionResponse,
    account_group_decision: AccountGroupDecision | None = None,
) -> QuestionInteraction:
    if interaction.status != "pending":
        raise ValueError("Question is no longer pending")
    if response.skipped and (response.answers or response.text is not None or account_group_decision):
        raise ValueError("A skipped response cannot also contain answers")
    _validate_account_group_decision(interaction, response, account_group_decision)
    parts = {part.part_id: part for part in interaction.parts}
    answered: set[str] = set()
    for answer in response.answers:
        part = parts.get(answer.part_id)
        if part is None:
            raise ValueError("Unknown question part")
        if answer.part_id in answered:
            raise ValueError("A response contains a duplicate part")
        answered.add(answer.part_id)
        if answer.choice_id is not None and answer.choice_id not in {choice.choice_id for choice in part.choices}:
            raise ValueError("Unknown choice for this part")
        if answer.choice_id is None and answer.text is None:
            raise ValueError("An answer requires a choice or text")
    decided: dict[str, Any] = {}
    if interaction.account_group_review is not None:
        decided["account_group_review"] = interaction.account_group_review.model_copy(
            update={"decision": account_group_decision}
        )
    if interaction.account_group_cancel is not None:
        decided["account_group_cancel"] = interaction.account_group_cancel.model_copy(
            update={"decision": account_group_decision}
        )
    return interaction.model_copy(
        update={
            "status": "resolved",
            "response": response.model_copy(deep=True),
            "resolved_at": datetime.now(UTC),
            **decided,
        }
    )


async def wait_for_interaction(ctx: CopilotContext, interaction: QuestionInteraction) -> QuestionInteraction:
    """Show one recorded interaction and return it once the user's response is accepted."""
    if ctx.workflow_copilot_chat_id is None or ctx.stream is None:
        raise ValueError("A Copilot question requires an active Copilot chat")
    repo = app.DATABASE.workflow_params
    chat_id = ctx.workflow_copilot_chat_id
    with pause_human_input(ctx, "question"):
        await repo.start_copilot_question(ctx.organization_id, chat_id, interaction)
        try:
            await ctx.stream.send(
                {
                    "type": "question_required",
                    "turn_id": ctx.turn_id,
                    "workflow_copilot_chat_id": chat_id,
                    "interactions": [interaction.model_dump(mode="json")],
                    "cancel_token": ctx.copilot_cancel_token,
                }
            )
            while True:
                recorded = await repo.poll_copilot_question(ctx.organization_id, chat_id, interaction.interaction_id)
                if recorded.status == "resolved":
                    await ctx.stream.send(
                        {
                            "type": "question_resolved",
                            "interaction": recorded.model_dump(mode="json"),
                            "continued": True,
                        }
                    )
                    return recorded
                if recorded.status != "pending":
                    raise asyncio.CancelledError("The question was cancelled or interrupted")
                await asyncio.sleep(QUESTION_POLL_SECONDS)
        finally:
            cleanup = asyncio.create_task(
                repo.interrupt_copilot_question(ctx.organization_id, chat_id, interaction.interaction_id)
            )
            _PENDING_QUESTION_CLEANUPS.add(cleanup)
            cleanup.add_done_callback(_release_question_cleanup)
            try:
                _, pending = await asyncio.wait({cleanup}, timeout=QUESTION_CLEANUP_TIMEOUT_SECONDS)
                if pending:
                    with contained_effect("question cleanup timeout log"):
                        LOG.warning("Copilot question cleanup timed out", interaction_id=interaction.interaction_id)
            finally:
                if not cleanup.done():
                    cleanup.cancel()


async def ask_user(ctx: CopilotContext, arguments: AskUserArguments, tool_call_id: str) -> dict[str, Any]:
    """Display one tool request and deliver its recorded response to this same invocation."""
    interaction = create_question_interaction(arguments, turn_id=ctx.turn_id, tool_call_id=tool_call_id, ctx=ctx)
    recorded = await wait_for_interaction(ctx, interaction)
    if ctx.request_policy is not None:
        ctx.request_policy.project_question_response_sites(recorded)
        ctx.allow_untested_workflow_draft = ctx.request_policy.raw_secret_redacted_draft
    if recorded.response is not None and not recorded.response.skipped:
        ctx.credential_recovery_armed = True
    return recorded.tool_result()
