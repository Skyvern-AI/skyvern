from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, cast

import structlog
from mcp.types import CallToolResult, TextContent

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.cache.base import BaseCache
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.credential_pause import apply_connected_credential_to_policy
from skyvern.forge.sdk.copilot.credential_resolution import safe_admitted_url
from skyvern.forge.sdk.copilot.demonstration_recorder import (
    DEMONSTRATION_CANCELLED_TTL,
    DEMONSTRATION_TTL,
    DemonstrationRecord,
    DemonstrationRejection,
    decode_demonstration_record,
    demonstration_event_key,
    demonstration_lock_key,
    demonstration_record_key,
    encode_demonstration_record,
    expire_demonstration_payload,
    finish_shielded,
    safe_label,
    store_settled_demonstration,
)
from skyvern.forge.sdk.copilot.enforcement import NUDGE_SENTINEL
from skyvern.forge.sdk.copilot.llm_config import resolve_raw_secret_safety_handler
from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer
from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm, summarize_tool_result
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy, screen_raw_secret_safety
from skyvern.forge.sdk.copilot.runtime import DemonstratedAction, ScoutedInteraction, staged_demonstrated_action
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure
from skyvern.forge.sdk.copilot.streaming_adapter import parse_tool_output
from skyvern.forge.sdk.copilot.tools.scouting import record_scouted_interaction_fact
from skyvern.forge.sdk.experimentation.copilot_demonstration import shared_copilot_demonstration_cache

LOG = structlog.get_logger()

DEMONSTRATION_DONE_INSTRUCTION = (
    "The browser actions above are the user's demonstration of this task, not your own. A value shown as not "
    "recorded was withheld by the secret screen; a replay gap states when the screen could not check it."
    "{insertion}"
)
_TYPED_VALUE_SCREEN_CONCURRENCY = 4


async def replay_demonstration(ctx: CopilotContext, mcp_server: SkyvernOverlayMCPServer) -> list[dict[str, Any]]:
    cache = shared_copilot_demonstration_cache()
    demonstration_id = ctx.demonstration_id
    if cache is None or not demonstration_id:
        return []
    record = await _claim_replay(cache, ctx, demonstration_id)
    replay_token = record.replay_token
    if replay_token is None:
        raise DemonstrationRejection(HTTPStatus.CONFLICT, "Demonstration replay lease is unavailable")
    try:
        events = await _load_events(cache, ctx, demonstration_id, record.event_count)
        history = await _replay_events(ctx, mcp_server, events, record)
    except BaseException:
        await finish_shielded(_finish_replay(cache, ctx, demonstration_id, replay_token, consumed=False))
        raise
    await _finish_replay(cache, ctx, demonstration_id, replay_token, consumed=True)
    return history


async def _replay_events(
    ctx: CopilotContext,
    mcp_server: SkyvernOverlayMCPServer,
    events: list[dict[str, Any]],
    record: DemonstrationRecord,
) -> list[dict[str, Any]]:
    final_url = record.final_url or ""
    raw_secret_safety_handler = await resolve_raw_secret_safety_handler(
        ctx.workflow_permanent_id,
        ctx.organization_id,
    )
    screen_started_at = time.monotonic()
    typed_screens = await _screen_typed_values(
        events,
        raw_secret_safety_handler,
        organization_id=ctx.organization_id,
    )
    screen_latency_ms = (time.monotonic() - screen_started_at) * 1000
    detected_count = sum(screen.status == "detected" for screen in typed_screens.values())
    unavailable_count = sum(screen.status == "blocked" for screen in typed_screens.values())
    withheld_count = sum(
        not _screen_passed(screen, events[index].get("value")) for index, screen in typed_screens.items()
    )
    failure_kinds = sorted({screen.failure_kind for screen in typed_screens.values() if screen.failure_kind != "none"})

    history: list[dict[str, Any]] = []
    if unavailable_count:
        history.append(
            {
                "role": "user",
                "content": NUDGE_SENTINEL
                + f"The secret check was unavailable, so {unavailable_count} typed values were withheld.",
            }
        )
    call_counter = 0
    current_frame_path: tuple[tuple[str, str], ...] | None = None
    sensitive_count = 0
    credential_fill_count = 0

    def next_call_id() -> str:
        nonlocal call_counter
        call_counter += 1
        return f"call_demo_{ctx.turn_id}_{call_counter}"

    async def record_call(
        alias: str,
        real_args: dict[str, Any],
        model_args: dict[str, Any],
        output: CallToolResult | dict[str, Any],
    ) -> None:
        call_id = next_call_id()
        parsed = parse_tool_output(output.content if isinstance(output, CallToolResult) else output)
        if isinstance(output, CallToolResult) and output.isError and parsed.get("ok") is not False:
            parsed = {**parsed, "ok": False}
        safe_output = scrub_secrets_from_structure(ctx, sanitize_tool_result_for_llm(alias, parsed))
        output_text = _call_tool_result_text(output) or json.dumps(safe_output, separators=(",", ":"))
        history.extend(
            [
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": alias,
                    "arguments": json.dumps(model_args, separators=(",", ":")),
                },
                {"type": "function_call_output", "call_id": call_id, "output": output_text},
            ]
        )
        ctx.tool_calls_this_turn += 1
        ctx.tool_activity.append({"tool": alias, "summary": summarize_tool_result(alias, parsed)})

    async def replay_recorded_call(
        alias: str,
        real_args: dict[str, Any],
        model_args: dict[str, Any],
        action: DemonstratedAction,
    ) -> None:
        with staged_demonstrated_action(action):
            output = await mcp_server.call_tool(alias, real_args)
        await record_call(alias, real_args, model_args, output)

    async def _replay_frame_switch(alias: str, args: dict[str, Any], source_url: str) -> None:
        action = DemonstratedAction(
            selector=str(args.get("selector") or ""),
            source_url=source_url,
            packet=None,
            synthesized_result={
                "ok": True,
                "data": args,
                "browser_context": {"url": source_url, "title": ""},
            },
        )
        await replay_recorded_call(alias, args, args, action)

    for index, event in enumerate(events):
        kind = event.get("kind")
        if kind == "gap":
            reason = event.get("reason")
            if not isinstance(reason, str) or not reason:
                reason = "unknown"
            history.append(_gap_fact(event, reason))
            continue
        frame = event.get("frame")
        if isinstance(frame, dict):
            frame_path = _frame_path(frame)
            if frame_path is None:
                history.append(_gap_fact(event, "nested_frame"))
                continue
            if frame_path != current_frame_path:
                source_url = _safe_model_visible_url(_event_source_url(event))
                if current_frame_path is not None:
                    await _replay_frame_switch("skyvern_frame_main", {}, source_url)
                for name, selector in frame_path:
                    args = {"name": name} if name else {"selector": selector}
                    await _replay_frame_switch("skyvern_frame_switch", args, source_url)
            current_frame_path = frame_path

        if kind == "secret":
            sensitive_count += 1
            selector = str(event.get("selector") or "")
            field = event.get("field")
            is_select = event.get("control") == "select"
            args = {"selector": selector, "value" if is_select else "text": "<value not recorded>"}
            data: dict[str, Any] = {"value_recorded": False}
            # Only a field the page marked by type or autocomplete is named; anything else may be over-withheld.
            if field in {"password", "totp"}:
                data["sensitive_field"] = field
            field_label = safe_label(event.get("field_label"))
            if field_label:
                data["field_label"] = field_label
            output = {"ok": True, "data": data}
            await record_call("select_option" if is_select else "type_text", args, args, output)
            continue
        if kind == "credential_fill":
            credential_fill_count += 1
            credential_id = event.get("credential_id")
            credential_selector = event.get("selector")
            field = event.get("field")
            source_url = _safe_model_visible_url(_event_source_url(event))
            credentials = []
            if isinstance(credential_id, str):
                credentials = await app.DATABASE.credentials.get_credentials_by_ids(
                    [credential_id], organization_id=ctx.organization_id
                )
                policy = ctx.request_policy
                if credentials and isinstance(policy, RequestPolicy):
                    policy.credential_ask_login_page_urls = [source_url]
                    apply_connected_credential_to_policy(ctx, policy, credentials[0])
            scout_fact = event.get("scout_fact")
            if credentials and isinstance(scout_fact, dict) and isinstance(scout_fact.get("tool_name"), str):
                fact = cast(ScoutedInteraction, dict(scout_fact))
                fact["actor"] = "user"
                record_scouted_interaction_fact(ctx, fact)
            args = {
                "selector": credential_selector if isinstance(credential_selector, str) else "",
                "credential_id": credential_id if isinstance(credential_id, str) else "",
                "field": field if field in {"password", "totp"} else "password",
            }
            output = (
                event.get("result") or {"ok": False}
                if credentials
                else {"ok": False, "error": "The demonstrated saved credential no longer exists."}
            )
            await record_call("fill_credential_field", args, args, output)
            continue

        translated = _translate_action(
            ctx,
            event,
            events[index + 1] if index + 1 < len(events) else None,
            final_url,
            typed_screens.get(index),
        )
        if translated is None:
            history.append(_gap_fact(event, f"unsupported_event:{kind or 'unknown'}"))
            continue
        alias, real_args, model_args, action = translated
        await replay_recorded_call(alias, real_args, model_args, action)

    action_count = len(events)
    LOG.info(
        "copilot_demonstration_replayed",
        action_count=action_count,
        sensitive_count=sensitive_count,
        credential_fill_count=credential_fill_count,
        screen_detected_count=detected_count,
        screen_withheld_count=withheld_count,
        screen_unavailable_count=unavailable_count,
        screen_failure_kinds=failure_kinds,
        screen_latency_ms=round(screen_latency_ms, 3),
    )
    ctx.demonstration_items = [
        _demonstration_label(event, typed_screens.get(index)) for index, event in enumerate(events)
    ]
    insertion_data = record.insertion or {}
    after_label = safe_label(insertion_data.get("after_label"))
    before_label = safe_label(insertion_data.get("before_label"))
    bounds = [
        text
        for text in (f"after {after_label}" if after_label else "", f"before {before_label}" if before_label else "")
        if text
    ]
    insertion = f" The requested insertion bounds are {' and '.join(bounds)}." if bounds else ""
    instruction = DEMONSTRATION_DONE_INSTRUCTION.format(insertion=insertion)
    stop_reason = record.stop_reason
    if stop_reason is not None and stop_reason != "stopped":
        history.append(
            {
                "role": "user",
                "content": NUDGE_SENTINEL + f"The demonstration ended because {stop_reason.replace('_', ' ')}.",
            }
        )
    return history + [{"role": "user", "content": NUDGE_SENTINEL + instruction}]


def _screen_passed(screen: Any, value: Any) -> bool:
    return screen is not None and screen.status == "clean" and screen.canonical_user_message == value


async def _screen_typed_values(
    events: list[dict[str, Any]],
    raw_secret_safety_handler: LLMAPIHandler | None,
    *,
    organization_id: str,
) -> dict[int, Any]:
    semaphore = asyncio.Semaphore(_TYPED_VALUE_SCREEN_CONCURRENCY)

    async def screen(value: str) -> tuple[str, Any]:
        async with semaphore:
            return value, await screen_raw_secret_safety(
                value,
                raw_secret_safety_handler,
                organization_id=organization_id,
            )

    typed_values = {
        index: value
        for index, event in enumerate(events)
        if event.get("kind") == "type" and isinstance((value := event.get("value")), str)
    }
    results = dict(await asyncio.gather(*(screen(value) for value in set(typed_values.values()))))
    return {index: results[value] for index, value in typed_values.items()}


def _frame_path(frame: dict[str, Any]) -> tuple[tuple[str, str], ...] | None:
    if frame.get("is_main") is True:
        return ()
    raw_path = frame.get("path")
    if isinstance(raw_path, list):
        path: list[tuple[str, str]] = []
        for entry in raw_path:
            if not isinstance(entry, dict):
                return None
            name = str(entry.get("name") or "")
            selector = str(entry.get("owner_selector") or "")
            if not name and not selector:
                return None
            path.append((name, selector))
        return tuple(path) if len(path) == 1 else None
    name = str(frame.get("name") or "")
    selector = str(frame.get("owner_selector") or "")
    return ((name, selector),) if name or selector else None


async def _claim_replay(cache: BaseCache, ctx: CopilotContext, demonstration_id: str) -> DemonstrationRecord:
    record_key = demonstration_record_key(ctx.organization_id, demonstration_id)
    async with cache.get_lock(demonstration_lock_key(ctx.organization_id, demonstration_id)):
        record = decode_demonstration_record(await cache.get(record_key))
        if record is None or record.workflow_permanent_id != ctx.workflow_permanent_id:
            LOG.info("copilot_demonstration_replay_skipped", reason="unavailable")
            raise DemonstrationRejection(HTTPStatus.NOT_FOUND, "Unknown demonstration")
        await store_settled_demonstration(cache, ctx.organization_id, demonstration_id, record)
        if record.status in {"recording", "stopping"}:
            raise DemonstrationRejection(HTTPStatus.CONFLICT, "Demonstration is still recording")
        if record.status != "stopped":
            raise DemonstrationRejection(HTTPStatus.CONFLICT, "Demonstration has already ended")
        record.status = "replaying"
        record.replay_started_at = datetime.now(UTC)
        record.replay_token = secrets.token_urlsafe(24)
        await cache.set(record_key, encode_demonstration_record(record), ex=DEMONSTRATION_TTL)
    return record


async def _load_events(
    cache: BaseCache, ctx: CopilotContext, demonstration_id: str, event_count: int
) -> list[dict[str, Any]]:
    raw_events = await asyncio.gather(
        *(
            cache.get(demonstration_event_key(ctx.organization_id, demonstration_id, seq))
            for seq in range(1, event_count + 1)
        )
    )
    events: list[dict[str, Any]] = []
    for seq, raw in enumerate(raw_events, 1):
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            events.append({"seq": seq, "kind": "gap", "reason": "recorded_event_unavailable"})
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            events.append({"seq": seq, "kind": "gap", "reason": "recorded_event_unavailable"})
    return events


async def _finish_replay(
    cache: BaseCache,
    ctx: CopilotContext,
    demonstration_id: str,
    replay_token: str,
    *,
    consumed: bool,
) -> None:
    record_key = demonstration_record_key(ctx.organization_id, demonstration_id)
    async with cache.get_lock(demonstration_lock_key(ctx.organization_id, demonstration_id)):
        record = decode_demonstration_record(await cache.get(record_key))
        if record is None or record.status != "replaying" or record.replay_token != replay_token:
            return
        record.status = "consumed" if consumed else "stopped"
        record.replay_started_at = None
        record.replay_token = None
        await cache.set(
            record_key,
            encode_demonstration_record(record),
            ex=DEMONSTRATION_CANCELLED_TTL if consumed else DEMONSTRATION_TTL,
        )
        event_count = record.event_count
    if consumed:
        await expire_demonstration_payload(
            cache, ctx.organization_id, demonstration_id, event_count, DEMONSTRATION_CANCELLED_TTL
        )


def _translate_action(
    ctx: CopilotContext,
    event: dict[str, Any],
    next_event: dict[str, Any] | None,
    final_url: str,
    typed_screen: Any,
) -> tuple[str, dict[str, Any], dict[str, Any], DemonstratedAction] | None:
    kind = event.get("kind")
    selector = str(event.get("selector") or "")
    packet = event.get("packet")
    packet = packet if isinstance(packet, dict) else None
    source_url = _safe_model_visible_url(_event_source_url(event))
    result_url = _safe_model_visible_url(_event_source_url(next_event)) if next_event is not None else ""
    if not result_url:
        result_url = _safe_model_visible_url(final_url)
    readback: str | None = None
    if kind == "click" and selector:
        alias = "click"
        real_args = model_args = {"selector": selector}
        data: dict[str, Any] = {"selector": selector}
    elif kind == "type" and selector:
        value = event.get("value")
        if not isinstance(value, str):
            return None
        visible_value = value if _screen_passed(typed_screen, value) else "<value not recorded>"
        alias = "type_text"
        real_args = model_args = {"selector": selector, "text": visible_value}
        data = {"selector": selector, "text_length": len(value)}
        readback = visible_value if visible_value == value else None
    elif kind == "select" and selector:
        value = event.get("value")
        if not isinstance(value, str):
            return None
        alias = "select_option"
        real_args = {"selector": selector, "value": value}
        model_args = {"selector": selector, "value": redact_raw_secrets_for_prompt(value)}
        data = dict(model_args)
    elif kind == "key":
        key = event.get("key")
        if not isinstance(key, str) or not key:
            return None
        alias = "press_key"
        real_args = {"key": key}
        if selector:
            real_args["selector"] = selector
        model_args = dict(real_args)
        data = {"key": key, "selector": selector}
    elif kind == "navigate":
        url = event.get("url")
        if not isinstance(url, str) or not url:
            return None
        alias = "navigate_browser"
        real_args = {"url": url}
        visible_url = _safe_model_visible_url(url)
        model_args = {"url": visible_url}
        data = {"url": visible_url}
        result_url = visible_url
        if isinstance(ctx.request_policy, RequestPolicy):
            ctx.request_policy.project_demonstration_navigation(url)
    else:
        return None
    action = DemonstratedAction(
        selector=selector,
        source_url=source_url,
        packet=packet,
        synthesized_result={
            "ok": True,
            "data": data,
            "browser_context": {"url": result_url, "title": "", "url_changed": source_url != result_url},
        },
        readback=readback,
    )
    return alias, real_args, model_args, action


def _event_source_url(event: dict[str, Any] | None) -> str:
    if event is None:
        return ""
    source_url = event.get("source_url")
    return source_url if isinstance(source_url, str) else ""


def _model_visible_url(url: str) -> str:
    return safe_admitted_url(url) or url


def _safe_model_visible_url(url: str) -> str:
    return redact_raw_secrets_for_prompt(_model_visible_url(url))


def _gap_fact(event: dict[str, Any], reason: str) -> dict[str, str]:
    seq = event.get("seq")
    step = f"Demonstration step {seq}" if isinstance(seq, int) else "A demonstration step"
    return {
        "role": "user",
        "content": NUDGE_SENTINEL + f"{step} was not recorded because {reason.replace('_', ' ')}.",
    }


def _demonstration_label(event: dict[str, Any], typed_screen: Any = None) -> str:
    kind = event.get("kind")
    if kind == "select" and isinstance(event.get("value"), str):
        return f'Select "{safe_label(event["value"])}"'[:60]
    if kind == "secret" and event.get("control") == "select":
        return "Select a sensitive option"
    if kind in {"type", "secret"}:
        value = event.get("value")
        if kind == "type" and isinstance(value, str) and _screen_passed(typed_screen, value):
            return f'Type "{safe_label(value)}"'[:60]
        packet = event.get("packet")
        role_name = packet.get("role_name") if isinstance(packet, dict) else None
        accessible_name = role_name.get("accessible_name") if isinstance(role_name, dict) else None
        target = safe_label(accessible_name) or safe_label(str(event.get("selector") or "field"))
        return f'Type into "{target}"'[:60]
    label = event.get("label")
    if isinstance(label, str) and label:
        return safe_label(label)
    if kind == "gap":
        return "Step not recorded"
    return "Action"


def _call_tool_result_text(output: CallToolResult | dict[str, Any]) -> str | None:
    if not isinstance(output, CallToolResult):
        return None
    for item in output.content:
        if isinstance(item, TextContent):
            return item.text
    return None
