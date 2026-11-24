"""Deliver messages the user sends into a running Copilot turn at the next model-call boundary."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.ember.context import EmberContext

if TYPE_CHECKING:
    from agents.result import RunResultStreaming

LOG = structlog.get_logger()

STEER_DOORBELL_TTL = timedelta(minutes=30)
STEER_POLL_SECONDS = 1.0


EMBER_STEER_KEY_PREFIX = "copilot_steer"


def ember_steer_key(organization_id: str, cancel_token: str) -> str:
    return f"{EMBER_STEER_KEY_PREFIX}:{organization_id}:{cancel_token}"


def _steer_target(ctx: object) -> tuple[EmberContext, str, str] | None:
    if not isinstance(ctx, EmberContext) or not ctx.workflow_copilot_chat_id or not ctx.ember_cancel_token:
        return None
    return ctx, ctx.workflow_copilot_chat_id, ctx.ember_cancel_token


async def _unhandled_doorbell(ctx: EmberContext, cancel_token: str) -> str | None:
    try:
        steer_id = await app.CACHE.get(ember_steer_key(ctx.organization_id, cancel_token))
    except Exception:
        LOG.debug("Ember steer doorbell read failed", exc_info=True)
        return None
    if isinstance(steer_id, bytes):
        steer_id = steer_id.decode()
    if not steer_id or steer_id in ctx.handled_steer_ids:
        return None
    return steer_id


async def watch_for_steer(result: RunResultStreaming, ctx: object) -> None:
    """End the stream once the user sends a message: abort an in-flight model call, or let running tools finish.

    An in-flight call has nothing in the session yet, so aborting it loses only that call, while a tool's
    output must reach the model.
    """
    target = _steer_target(ctx)
    if target is None:
        return
    ember_ctx, chat_id, cancel_token = target
    model_calls_before_run = ember_ctx.model_calls_this_turn
    requested = False
    while True:
        await asyncio.sleep(STEER_POLL_SECONDS)
        if not requested:
            steer_id = await _unhandled_doorbell(ember_ctx, cancel_token)
            if steer_id is None:
                continue
            try:
                waiting = await app.DATABASE.workflow_params.undelivered_ember_steer_ids(
                    ember_ctx.organization_id, chat_id, ember_ctx.turn_id
                )
            except Exception:
                LOG.debug("Ember steer record read failed; will retry", exc_info=True)
                continue
            # The doorbell is keyed by cancel token, so it can name a message this turn never recorded.
            if steer_id not in waiting:
                ember_ctx.handled_steer_ids.add(steer_id)
                continue
            requested = True
            LOG.info(
                "ember_steer_requested",
                steer_id=steer_id,
                turn_id=ember_ctx.turn_id,
                model_call_in_flight=ember_ctx.model_call_in_flight,
            )
        # The SDK saves a run's input only when its first model call starts, so stopping earlier drops it.
        if ember_ctx.model_calls_this_turn == model_calls_before_run:
            continue
        # A model call can start after the soft stop, so keep checking until the stream ends.
        if ember_ctx.model_call_in_flight and not ember_ctx.model_call_streamed_tool_call:
            # The aborted call never saw the frame it claimed, so the next call claims it again.
            ember_ctx.pending_frame_lease = None
            result.cancel(mode="immediate")
            return
        result.cancel(mode="after_turn")


async def take_steer_input(ctx: object) -> list[dict[str, Any]]:
    """Mark the turn's undelivered messages delivered and return them as the model's next user input.

    Only a doorbell this turn has not handled reaches the database, so a turn nobody steers never touches it.
    """
    target = _steer_target(ctx)
    if target is None:
        return []
    ember_ctx, chat_id, cancel_token = target
    doorbell = await _unhandled_doorbell(ember_ctx, cancel_token)
    if doorbell is None:
        return []
    steers = await app.DATABASE.workflow_params.take_ember_steer_messages(
        ember_ctx.organization_id, chat_id, ember_ctx.turn_id
    )
    ember_ctx.handled_steer_ids.add(doorbell)
    if not steers:
        return []
    for steer in steers:
        ember_ctx.handled_steer_ids.add(steer.steer_id)
        ember_ctx.steered_by_another = ember_ctx.steered_by_another or not steer.from_starter
        if ember_ctx.request_policy is not None:
            ember_ctx.request_policy.project_steer_message(steer)
    LOG.info(
        "ember_steer_delivered",
        turn_id=ember_ctx.turn_id,
        steer_ids=[steer.steer_id for steer in steers],
    )
    await ember_ctx.stream.send(
        {
            "type": "steer_delivered",
            "turn_id": ember_ctx.turn_id,
            "steer_messages": [steer.model_dump(mode="json") for steer in steers],
        }
    )
    return [{"role": "user", "content": steer.text} for steer in steers]
