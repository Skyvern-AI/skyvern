from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict, Field

from skyvern.forge.sdk.copilot.human_input_wait import pause_human_input
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_text
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


class QuestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[QuestionAnswer] = Field(default_factory=list)
    text: str | None = None
    skipped: bool = False


class QuestionInteraction(BaseModel):
    interaction_id: str
    turn_id: str
    tool_call_id: str
    parts: list[QuestionPart]
    status: Literal["pending", "resolved", "cancelled", "interrupted"] = "pending"
    response: QuestionResponse | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None

    def tool_result(self) -> dict[str, Any]:
        if self.status != "resolved" or self.response is None:
            raise ValueError("Question has no accepted response")
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


def resolve_question_response(interaction: QuestionInteraction, response: QuestionResponse) -> QuestionInteraction:
    if interaction.status != "pending":
        raise ValueError("Question is no longer pending")
    if response.skipped and (response.answers or response.text is not None):
        raise ValueError("A skipped response cannot also contain answers")
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
    return interaction.model_copy(
        update={
            "status": "resolved",
            "response": response.model_copy(deep=True),
            "resolved_at": datetime.now(UTC),
        }
    )


async def ask_user(ctx: CopilotContext, arguments: AskUserArguments, tool_call_id: str) -> dict[str, Any]:
    """Display one tool request and deliver its recorded response to this same invocation."""
    from skyvern.forge import app

    if ctx.workflow_copilot_chat_id is None or ctx.stream is None:
        raise ValueError("ask_user requires an active Copilot chat")
    repo = app.DATABASE.workflow_params
    chat_id = ctx.workflow_copilot_chat_id
    interaction = create_question_interaction(arguments, turn_id=ctx.turn_id, tool_call_id=tool_call_id, ctx=ctx)
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
                    return recorded.tool_result()
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
