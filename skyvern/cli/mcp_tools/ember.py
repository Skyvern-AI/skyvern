"""Skyvern MCP tools that start, watch, steer and stop Ember turns. send reads the chat-post stream only until
turn_start and every later read comes from chat-history, so nothing is kept between calls."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import yaml
from playwright.async_api import FilePayload
from pydantic import BaseModel, ConfigDict, Field

from skyvern.client.errors import NotFoundError

from ._common import ErrorCode, Timer, make_error, make_result
from ._ember_http import (
    ChatScanCapped,
    EmberError,
    HistoryReadTimeout,
    SitesChanged,
    WriteUnconfirmed,
    cancel,
    chat_id_of,
    get_artifact_url,
    get_browser_session,
    get_debug_session_browser_id,
    get_history,
    list_all_chats,
    list_chats,
    list_saved_logins,
    post_action,
    post_chat,
    recovery_token,
    steer,
    upload_file,
)
from ._session import is_stdio_local_file_access_enabled
from ._workflow_http import create_workflow_raw, get_workflow_by_id
from .browser import read_local_file_under

MAX_WAIT_SECONDS = 45
POLL_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 10.0
# chat-post must still reach turn_start when wait_seconds is 0, so its read gets at least this long.
TURN_START_MIN_SECONDS = 15.0
SUMMARY_LIMIT_CHARS = 8000
DETAIL_LIMIT_CHARS = 60000
TEST_END_TO_END_MESSAGE = "Test this workflow end to end."
MAX_ATTACHED_FILES = 20
MAX_ATTACHED_FILE_BYTES = 30 * 1024 * 1024
_MAX_ENTRIES = 15
_MAX_ENTRY_CHARS = 300
_MAX_REPLY_CHARS = 2000
_MAX_USER_MESSAGE_CHARS = 500
_MAX_SAVED_LOGINS = 25
_HIDDEN_ACTIVITY_TOOLS = frozenset({"get_run_results", "get_browser_screenshot"})
_STOPPED_TERMINAL_REASONS = frozenset({"cancel", "user_cancelled"})
_INTERRUPTED_TERMINAL_REASON = "interrupted"
# auto_applicable is a fully tested draft; with auto-accept off, the default, it still waits for Accept.
_PROPOSAL_TESTED = {"review_tested": True, "review_untested": False, "auto_applicable": True}
# Ids, links and the cursor are what the caller acts on next, so the size cap never clips them.
_UNCLIPPED_KEYS = frozenset(
    {
        "item_id",
        "link",
        "links",
        "cursor",
        "next",
        "ember_chat_id",
        "workflow_id",
        "turn_id",
        "proposal_id",
        "part_id",
        "choice_id",
        "credential_id",
        # Answering a site question with "add" saves this exact site to the login on the terms the consent states,
        # so neither is shown clipped.
        "site",
        "consent",
    }
)
LOGIN_ITEM_PREFIX = "login:"
TRIGGER_ITEM_PREFIX = "trigger:"
_SEND_ITEM_PREFIXES = ("choose_account:", "connect:")
_DRAFT_UNREVIEWED_REASON = (
    "Approving this run first saves Ember's draft, whose full review isn't shown here, so the user approves it in "
    "the Studio at links.studio; it can only be declined through MCP."
)
_TRIGGER_REASON = "This trigger was created turned off; turn it on in the Studio's Triggers panel."
_TOO_LARGE_REASON = "Too large to show whole here, so it allows no decision through MCP."

_TOO_LARGE_NEXT = (
    "The items waiting on the user were too large to show in full here, so they allow no decision through MCP; "
    "send the user to links.studio to answer them."
)
_ACCEPT_NEXT = (
    "Show the user Ember's reply and proposal and ask whether to accept it. Once they choose, call "
    "skyvern_ember_decide with decision 'accept' or 'reject' and item_id=proposal.proposal_id, or "
    "skyvern_ember_send to ask for changes."
)
_PROPOSAL_TOO_LARGE_NEXT = (
    "Ember's proposal was too large to show in full here, so it can't be accepted or rejected through MCP. "
    "Show the user Ember's reply; they review the proposal in the Studio at links.studio, or call "
    "skyvern_ember_send to ask for changes."
)
_PRESELECTED_NOTE = (
    "An account with preselected: false already had a group run that may have taken effect, so it needs the "
    "user's explicit yes."
)
_VOLATILE_WORKFLOW_KEYS = frozenset(
    {"workflow_id", "workflow_permanent_id", "organization_id", "version", "created_at", "modified_at", "deleted_at"}
)
_STUDIO_BLANK_WORKFLOW: dict[str, Any] = {
    "title": "New Agent",
    "description": "",
    "ai_fallback": True,
    "code_version": 2,
    "mask_secrets": True,
    "run_with": "agent",
    "workflow_definition": {"version": 2, "blocks": [], "parameters": []},
}

State = Literal["idle", "working", "waiting_for_user", "finished", "stopped", "interrupted", "failed"]

_HINTS = {
    ErrorCode.TURN_IN_PROGRESS: (
        "Ember is already working on this workflow. Call skyvern_ember_check to watch it, skyvern_ember_send with "
        "if_turn_running='steer' on that chat to add to it, or skyvern_ember_stop if the user wants it stopped."
    ),
    ErrorCode.TURN_CONTROL_DENIED: (
        "Only the person who started this Ember turn can steer or answer it, and only they or an Admin can stop it. "
        "Tell the user."
    ),
    ErrorCode.CHAT_NOT_FOUND: "Call skyvern_ember_list to find the chat's ember_chat_id.",
    ErrorCode.EMBER_UNAVAILABLE: "Upgrade the Skyvern server to a version that serves Ember's API.",
    ErrorCode.PERMISSION_DENIED: "Ask an Admin for the workflow.update and workflow_copilot permissions.",
    ErrorCode.RETRY_LATER: "Retry the call in a few seconds.",
    ErrorCode.INVALID_INPUT: "Fix the arguments and call again.",
    ErrorCode.WORKFLOW_NOT_FOUND: "Call skyvern_workflow_list to find the workflow's wpid_ id.",
    ErrorCode.RUN_NOT_FOUND: "Call skyvern_workflow_run_list to find a wr_ run of this workflow.",
    ErrorCode.ITEM_EXPIRED: (
        "The item is no longer waiting. Call skyvern_ember_check to see what Ember is waiting on now, and follow next."
    ),
    ErrorCode.PROPOSAL_CHANGED: (
        "Ember's draft changed since the user reviewed it. Call skyvern_ember_check, show the user the new proposal, "
        "and decide only after they choose again."
    ),
    ErrorCode.NO_PROPOSAL: "There is no proposal to accept or reject. Call skyvern_ember_check to see the chat.",
}


def _error(
    action: str, code: str, message: str, timer: Timer, *, data: dict[str, Any] | None = None, **details: str
) -> dict[str, Any]:
    return make_result(
        action,
        ok=False,
        data=data,
        timing_ms=timer.timing_ms,
        error=make_error(code, message, _HINTS.get(code, "Check the Skyvern server and API key."), details or None),
    )


def _ember_error(action: str, exc: EmberError, timer: Timer, *, data: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(exc, WriteUnconfirmed):
        return make_result(
            action,
            ok=False,
            data=data,
            timing_ms=timer.timing_ms,
            error=make_error(
                exc.code,
                exc.message,
                "Call skyvern_ember_check to see whether it took effect before calling this tool again.",
            ),
        )
    if exc.retry_after is not None:
        return _error(action, exc.code, exc.message, timer, data=data, retry_after=exc.retry_after)
    return _error(action, exc.code, exc.message, timer, data=data)


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _clip(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit} characters omitted]"


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _rows(history: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in history.get("chat_history") or [] if isinstance(row, dict)]


def _offered_accounts(history: dict[str, Any] | None) -> list[Any]:
    payload = ((_last_ai_row(_rows(history)) if history else None) or {}).get("narrative_payload") or {}
    choices = payload.get("connectedAccountChoices") or []
    return [choice.get("connection_id") for choice in choices if isinstance(choice, dict)]


def _last_ai_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((row for row in reversed(rows) if row.get("sender") == "ai"), None)


def _links(history: dict[str, Any]) -> dict[str, str | None]:
    app_url = history.get("app_url")
    wpid = history.get("workflow_permanent_id")
    chat_id = chat_id_of(history)
    if not app_url:
        return {"studio": None, "credentials": None, "integrations": None}
    return {
        "studio": f"{app_url}/agents/{wpid}/studio?chat={chat_id}" if wpid and chat_id else None,
        "credentials": f"{app_url}/credentials",
        "integrations": f"{app_url}/integrations",
    }


def _group_review_warnings(review: dict[str, Any]) -> list[str]:
    # The same warnings, on the same conditions, as the Studio's account-group review card.
    if review.get("single_run") or review.get("row_group"):
        return []
    warnings = []
    if review.get("credential_parameter_used_by") == []:
        warnings.append(
            f"No step of this workflow lists {review.get('credential_parameter_key')}, so each run may not sign in as "
            "its own account."
        )
    if not review.get("clean_group_run_id"):
        warnings.append("This version has not yet completed cleanly in a group for any selected account.")
    return warnings


def _question_action(item: dict[str, Any], expires_at: str | None, studio: str | None) -> dict[str, Any]:
    action: dict[str, Any] = {"item_id": item.get("interaction_id"), "expires_at": expires_at, "link": studio}
    if review := item.get("account_group_review"):
        rows = [row for row in review.get("rows") or [] if isinstance(row, dict)]
        action |= {
            "kind": "run_approval",
            "prompt": review.get("action_summary"),
            "allowed_decisions": ["approve", "decline"],
            "facts": {
                "workflow_title": review.get("workflow_title"),
                "version": review.get("version"),
                "single_run": review.get("single_run", False),
                "row_group": review.get("row_group", False),
                "max_concurrency": review.get("max_concurrency"),
                "tested_workflow_run_id": review.get("tested_workflow_run_id"),
                "saves_draft": review.get("saves_draft", False),
                "draft_proposal_id": f"{review['proposal_owner_turn_id']}:{review['proposal_revision']}"
                if review.get("saves_draft")
                and review.get("proposal_owner_turn_id")
                and review.get("proposal_revision")
                else None,
                "draft_tested": review.get("draft_tested", False),
                # A full page must fit the summary limit, so fields the row does not set are left out.
                "accounts": [
                    {
                        "credential_id": row.get("credential_id"),
                        # An input row's label only restates its inputs.
                        **({} if row.get("inputs") else {"label": row.get("label")}),
                        "preselected": row.get("preselected", True),
                        **{key: row[key] for key in ("prior_outcome", "inputs", "parameter_key") if row.get(key)},
                    }
                    for row in rows
                ],
                "total_accounts": len(rows),
                "common_inputs": review.get("common_inputs") or {},
                **(
                    {"preselected_note": _PRESELECTED_NOTE}
                    if any(not row.get("preselected", True) for row in rows)
                    else {}
                ),
                "cost": review.get("cost"),
                "warnings": _group_review_warnings(review),
            },
        }
    elif cancel_review := item.get("account_group_cancel"):
        # The card lists at most one page of rows, but approval cancels every unfinished run in the group.
        unfinished = cancel_review.get("unfinished_count")
        action |= {
            "kind": "run_group_cancel",
            "prompt": f"Stop all {unfinished} unfinished runs in this account group?"
            if unfinished is not None
            else "Stop every unfinished run in this account group?",
            "allowed_decisions": ["approve", "decline"],
            "facts": {
                "unfinished_runs": unfinished,
                "listed_runs": [
                    {
                        "label": row.get("label"),
                        "workflow_run_id": row.get("workflow_run_id"),
                        "run_status": row.get("run_status"),
                    }
                    for row in cancel_review.get("unfinished_rows") or []
                    if isinstance(row, dict)
                ],
            },
        }
    elif delete_review := item.get("credential_delete_review"):
        action |= {
            "kind": "credential_deletion",
            "prompt": "Delete these saved credentials?",
            "allowed_decisions": ["approve", "decline"],
            "facts": {
                "credentials": [
                    {"credential_id": row.get("credential_id"), "name": row.get("name")}
                    for row in delete_review.get("rows") or []
                    if isinstance(row, dict)
                ],
                "organization_credential_count": delete_review.get("total_credential_count"),
            },
        }
    else:
        parts = [part for part in item.get("parts") or [] if isinstance(part, dict)]
        action |= {
            "kind": "question",
            # A one-line summary; each part's own prompt and choices below are shown whole, so only fitting can cut
            # them, and a question it cuts can't be answered through MCP.
            "prompt": _clip(" ".join(str(part.get("prompt") or "") for part in parts), 1000),
            "options": [
                {
                    "part_id": part.get("part_id"),
                    "prompt": part.get("prompt"),
                    "choices": [
                        {
                            "choice_id": choice.get("choice_id"),
                            "text": choice.get("text"),
                            "recommended": choice.get("recommended", False),
                            **({"detail_prompt": choice["detail_prompt"]} if choice.get("detail_prompt") else {}),
                        }
                        for choice in part.get("choices") or []
                        if isinstance(choice, dict)
                    ],
                }
                for part in parts
            ],
        }
    return action


def _login_decisions(card: dict[str, Any]) -> list[str]:
    decisions = ["use_saved_login", "skip"]
    registration = card.get("registration")
    if isinstance(registration, dict) and not registration.get("attempted"):
        decisions.append("generate_login")
    if card.get("sign_in_browser_session_id"):
        decisions.append("sign_in_myself")
    if card.get("signing_in"):
        decisions.append("signed_in")
    return decisions


def _site_question_facts(question: dict[str, Any]) -> dict[str, Any]:
    site = question.get("site")
    return {
        "credential_id": question.get("credential_id"),
        "name": question.get("name"),
        "site": site,
        "saved_sites": (question.get("saved_sites") or [])[:10],
        "vault_sourced": bool(question.get("vault_sourced")),
        "consent": (
            f"Allowing lets Skyvern type this login's password on {site}, in this chat and in every saved run that "
            "uses it."
        ),
    }


def _login_action(card: dict[str, Any], credentials_link: str | None) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "reason": card.get("reason"),
        "login_page_urls": (card.get("login_page_urls") or [])[:5],
        "named_credential_id": card.get("named_credential_id"),
        "signing_in": bool(card.get("signing_in")),
    }
    if isinstance(registration := card.get("registration"), dict):
        facts["registration"] = {
            "username": registration.get("username"),
            "credential_name": registration.get("credential_name"),
            "attempted": bool(registration.get("attempted")),
            "outcome": registration.get("outcome"),
        }
    if isinstance(question := card.get("site_question"), dict):
        facts["site_question"] = _site_question_facts(question)
    return {
        "item_id": _login_item_id(card),
        "kind": "login_needed",
        "prompt": _clip(card.get("message"), 1000),
        "allowed_decisions": _login_decisions(card),
        "facts": facts,
        "expires_at": card.get("expires_at"),
        "link": credentials_link,
    }


def _trigger_actions(last_ai: dict[str, Any] | None, studio: str | None) -> list[dict[str, Any]]:
    payload = (last_ai or {}).get("narrative_payload") or {}
    return [
        {
            "item_id": f"{TRIGGER_ITEM_PREFIX}{trigger.get('eventTriggerId')}",
            "kind": "trigger_created",
            "prompt": f"Ember created a {trigger.get('sourceKind')} trigger. It is off.",
            "allowed_decisions": [],
            "facts": {
                "source_kind": trigger.get("sourceKind"),
                "filters": [
                    {"label": row.get("label"), "value": row.get("value")}
                    for row in (trigger.get("filters") or [])[:10]
                    if isinstance(row, dict)
                ],
                "fires_on_its_own": bool(trigger.get("firesOnItsOwn")),
                "connection_id": trigger.get("connectionId"),
            },
            "reason": _TRIGGER_REASON,
            "expires_at": None,
            "link": studio,
        }
        for trigger in payload.get("createdTriggers") or []
        if isinstance(trigger, dict)
    ]


def _claimed_deletion(item: dict[str, Any]) -> bool:
    review = item.get("credential_delete_review")
    return isinstance(review, dict) and review.get("claimed_at") is not None


def _user_actions(
    history: dict[str, Any],
    last_ai: dict[str, Any] | None,
    *,
    turn_ended: bool,
    links: dict[str, str | None],
) -> list[dict[str, Any]]:
    in_flight = history.get("in_flight_turn") or {}
    actions = [
        _question_action(item, in_flight.get("question_open_until"), links["studio"])
        for item in history.get("question_interactions") or []
        if isinstance(item, dict) and item.get("status") == "pending" and not _claimed_deletion(item)
    ]
    for card in history.get("pending_credential_requests") or []:
        if isinstance(card, dict):
            actions.append(_login_action(card, links["credentials"]))
    payload = (last_ai or {}).get("narrative_payload") or {}
    if turn_ended and payload:
        turn_id = payload.get("turnId")
        choices = [choice for choice in payload.get("connectedAccountChoices") or [] if isinstance(choice, dict)]
        if choices:
            actions.append(
                {
                    "item_id": f"choose_account:{turn_id}",
                    "kind": "choose_account",
                    "prompt": "Which connected account should Ember use?",
                    "options": [
                        {
                            "connection_id": choice.get("connection_id"),
                            "name": choice.get("name"),
                            "email": choice.get("email_address"),
                        }
                        for choice in choices[:10]
                    ],
                    "facts": {},
                    "expires_at": None,
                    "link": links["studio"],
                }
            )
        for card in payload.get("connectionCards") or []:
            if not isinstance(card, dict):
                continue
            actions.append(
                {
                    "item_id": f"connect:{turn_id}:{card.get('integrationId')}",
                    "kind": "connect_account",
                    "prompt": f"Connect a {card.get('integrationName')} account.",
                    "facts": {"integration": card.get("integrationName"), "provider": card.get("provider")},
                    "expires_at": None,
                    "link": links["integrations"],
                }
            )
    return actions


def _turn_state(
    history: dict[str, Any],
    rows: list[dict[str, Any]],
    last_ai: dict[str, Any] | None,
    has_pending_items: bool,
    login_resolved: bool,
) -> tuple[State, str | None]:
    in_flight = history.get("in_flight_turn")
    if in_flight is not None:
        if has_pending_items:
            return "waiting_for_user", "you"
        paused_for = (in_flight.get("narrative") or {}).get("pausedFor")
        pending = [
            item
            for item in history.get("question_interactions") or []
            if isinstance(item, dict) and item.get("status") == "pending"
        ]
        # A caller without control sees no cancel_token, or a hidden question flagged by pending_question_cancel_token.
        # A credential pause without a card waits on no one only in the read right after this caller's login decision.
        has_control = in_flight.get("cancel_token") is not None
        question_just_resolved = (
            paused_for == "question"
            and has_control
            and (
                not history.get("pending_question_cancel_token")
                or (bool(pending) and all(_claimed_deletion(item) for item in pending))
            )
        )
        credential_just_resolved = (
            paused_for == "credential"
            and login_resolved
            and has_control
            and not history.get("pending_credential_requests")
        )
        if paused_for and not question_just_resolved and not credential_just_resolved:
            return "waiting_for_user", "the person who started this turn"
        return "working", None
    if has_pending_items:
        return "waiting_for_user", "you"
    if not rows:
        return "idle", None
    if rows[-1].get("sender") == "user" or last_ai is None:
        return "working", None
    outcome = last_ai.get("turn_outcome") or {}
    payload = last_ai.get("narrative_payload") or {}
    terminal_reason = outcome.get("terminal_reason")
    if terminal_reason == _INTERRUPTED_TERMINAL_REASON:
        return "interrupted", None
    if payload.get("cancelled") or terminal_reason in _STOPPED_TERMINAL_REASONS:
        return "stopped", None
    if payload.get("terminal") == "error":
        return "failed", None
    return "finished", None


def _cursor(turn_id: str | None, entry: dict[str, Any]) -> str:
    return f"{turn_id}:{entry.get('id')}"


def _progress(history: dict[str, Any], narrative: dict[str, Any], since: str | None) -> dict[str, Any]:
    turn_id = narrative.get("turnId")
    activity = [
        entry
        for entry in narrative.get("designActivity") or []
        if isinstance(entry, dict) and entry.get("toolName") not in _HIDDEN_ACTIVITY_TOOLS
    ]
    start = 0
    if since is not None:
        start = next((index + 1 for index, entry in enumerate(activity) if _cursor(turn_id, entry) == since), 0)
    fresh = activity[start:][-_MAX_ENTRIES:]
    blocks = [block for block in narrative.get("blocks") or [] if isinstance(block, dict)]
    draft = narrative.get("draft") or {}
    work_plan = history.get("work_plan") or (narrative.get("workPlan") or {}).get("items") or []
    return {
        "cursor": _cursor(turn_id, fresh[-1]) if fresh else since,
        "entries": [
            {
                "at": entry.get("timestamp") or entry.get("activityStartedAt"),
                "text": _clip(entry.get("text") or entry.get("displayLabel"), _MAX_ENTRY_CHARS),
                "ok": entry.get("success"),
                "workflow_run_id": entry.get("workflowRunId"),
            }
            for entry in fresh
        ],
        "work_plan": work_plan[:20],
        "blocks": [
            {"label": block.get("label"), "state": block.get("state"), "workflow_run_id": block.get("workflowRunId")}
            for block in blocks[:30]
        ],
        "draft_blocks": (draft.get("blockLabels") or [])[:30],
        "live_browser_url": None,
        "screenshot_url": None,
    }


def _proposal(history: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not history.get("proposed_workflow"):
        return None
    metadata = history.get("proposed_workflow_metadata") or {}
    owner_turn_id = metadata.get("owner_turn_id")
    # Another turn's review would describe a different draft, so a summary outside the window is left unknown.
    owner_row = next(
        (
            row
            for row in reversed(rows)
            if owner_turn_id and row.get("sender") == "ai" and row.get("turn_id") == owner_turn_id
        ),
        None,
    )
    run = history.get("proposed_workflow_run")
    revision = metadata.get("revision")
    disposition = str(metadata.get("disposition"))
    proposal: dict[str, Any] = {
        "proposal_id": None,
        "status": metadata.get("disposition"),
        "tested": _PROPOSAL_TESTED.get(disposition),
        "test_run": {"workflow_run_id": run.get("workflow_run_id"), "status": run.get("status")} if run else None,
        "review": "missing",
        "changes": None,
        "input_changes": None,
        "duplicate_writes": None,
        "auto_accept": history.get("auto_accept"),
    }
    if owner_row is not None:
        payload = owner_row.get("narrative_payload") or {}
        facts = payload.get("turnFacts") or {}
        proposal["tested"] = _PROPOSAL_TESTED.get(disposition, facts.get("ranCleanOnCurrentSource"))
        # Accepting saves the whole draft, so only a draft whose review can be shown gets an id to accept by.
        if review := payload.get("review"):
            proposal |= {
                "proposal_id": f"{owner_turn_id}:{revision}" if revision else None,
                "review": "shown",
                "changes": [
                    {
                        "label": block.get("label"),
                        "change": block.get("change"),
                        "never_tested": block.get("neverTested"),
                    }
                    for block in review.get("blocks") or []
                    if isinstance(block, dict)
                ],
                "input_changes": review.get("inputs") or [],
                "duplicate_writes": [
                    {"block_type": group.get("blockType"), "block_labels": group.get("blockLabels")}
                    for group in review.get("duplicateWrites") or []
                    if isinstance(group, dict)
                ],
            }
    return proposal


def _runs(history: dict[str, Any], progress: dict[str, Any]) -> list[dict[str, Any]]:
    app_url = history.get("app_url")
    statuses: dict[str, str | None] = {}
    for block in progress["blocks"]:
        if block["workflow_run_id"]:
            statuses[block["workflow_run_id"]] = block["state"]
    if run := history.get("proposed_workflow_run"):
        statuses[run.get("workflow_run_id")] = run.get("status")
    return [
        {"workflow_run_id": run_id, "status": status, "url": f"{app_url}/runs/{run_id}" if app_url else None}
        for run_id, status in list(statuses.items())[-10:]
    ]


def _next(
    state: str,
    waiting_on: str | None,
    user_actions: list[dict[str, Any]],
    proposal: dict[str, Any] | None,
    cursor: str | None,
) -> str:
    if state == "working":
        since = f" with since={cursor!r}" if cursor else ""
        return f"Ember is working. Call skyvern_ember_check{since} to keep watching."
    if state == "waiting_for_user" and waiting_on != "you":
        return "Ember is waiting on the person who started this turn. Tell the user; only that person can answer."
    if user_actions:
        kind = user_actions[0]["kind"]
        if kind == "choose_account":
            return (
                "Ask the user which account to use, then call skyvern_ember_send with connected_account_id and no "
                "message."
            )
        if kind == "connect_account":
            return "Ask the user to connect the account at links.integrations, then call skyvern_ember_send."
        if kind == "question":
            return "Show the user Ember's question, then pass their answer to skyvern_ember_answer with its item_id."
        if user_actions[0].get("reason") == _DRAFT_UNREVIEWED_REASON:
            return f"Show the user what Ember is asking. {_DRAFT_UNREVIEWED_REASON}"
        return (
            "Show the user what Ember is asking and wait for their choice; then call skyvern_ember_decide with its "
            "item_id and one of its allowed_decisions. Never choose for the user."
        )
    if state == "idle":
        return "Nothing is running. Call skyvern_ember_send to ask Ember for something."
    if state in ("stopped", "interrupted", "failed"):
        return f"The turn {state}. Show the user Ember's reply, and call skyvern_ember_send to try again if they want."
    if proposal and proposal.get("status") in _PROPOSAL_TESTED:
        if proposal.get("proposal_id"):
            return _ACCEPT_NEXT
        return (
            "Show the user Ember's reply and proposal; they accept it in the Studio at links.studio, or call "
            "skyvern_ember_send to ask for changes."
        )
    return "Show the user Ember's reply. Call skyvern_ember_send for a follow-up."


def _clip_strings(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return _clip(value, limit)
    if isinstance(value, list):
        return [_clip_strings(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: item if key in _UNCLIPPED_KEYS else _clip_strings(item, limit) for key, item in value.items()}
    return value


def _acted_on(action: dict[str, Any]) -> list[Any]:
    facts = action.get("facts") or {}
    question = action.get("kind") == "question"
    return [
        facts.get("common_inputs"),
        [(account.get("label"), account.get("inputs")) for account in facts.get("accounts") or []],
        # Generating a login saves it under exactly this username and name.
        ((facts.get("registration") or {}).get("username"), (facts.get("registration") or {}).get("credential_name")),
        [credential.get("name") for credential in facts.get("credentials") or []],
        # An answer resolves the whole question, so its prompt and choices must be shown whole too.
        (action.get("prompt"), action.get("options")) if question else None,
    ]


def _fit(result: dict[str, Any]) -> dict[str, Any]:
    def size() -> int:
        return len(json.dumps(result, default=str))

    progress = result["progress"]
    while size() > SUMMARY_LIMIT_CHARS and progress["entries"]:
        progress["entries"].pop(0)
    for key, limit in (("reply", 500), ("runs", 3)):
        if size() <= SUMMARY_LIMIT_CHARS:
            break
        value = result.get(key)
        if isinstance(value, str):
            result[key] = _clip(value, limit)
        elif value:
            result[key] = value[:limit]
    for key in ("blocks", "draft_blocks", "work_plan"):
        if size() <= SUMMARY_LIMIT_CHARS:
            break
        progress[key] = progress[key][:5]
    review = (result["proposal"]["changes"], result["proposal"]["input_changes"]) if result["proposal"] else None
    actions_before = [json.dumps(action, default=str) for action in result["user_actions"]]
    if size() > SUMMARY_LIMIT_CHARS and result["proposal"]:
        for key in ("changes", "input_changes"):
            if result["proposal"][key] is not None:
                result["proposal"][key] = result["proposal"][key][:5]
    acted_on_before = {action.get("item_id"): _acted_on(action) for action in result["user_actions"]}
    for limit in (300, 100, 40):
        if size() <= SUMMARY_LIMIT_CHARS:
            break
        result = _clip_strings(result, limit)
    # A card whose own content was shortened says so; the rules below decide what it still allows here.
    for action, before in zip(result["user_actions"], actions_before):
        if json.dumps(action, default=str) != before:
            action["cut"] = True
    # Accepting saves every change in the draft, so a review cut down to fit leaves the decision to the Studio.
    proposal = result["proposal"]
    # Every block a duplicate-write warning names is also in changes, so comparing changes covers the warnings too.
    review_cut = bool(proposal and (proposal["changes"], proposal["input_changes"]) != review)
    if review_cut:
        proposal["review"] = "cut"
        proposal["proposal_id"] = None
        if result["next"] == _ACCEPT_NEXT:
            result["next"] = _PROPOSAL_TOO_LARGE_NEXT
    # Clipping a summary is fine, but approving clipped run inputs or credential names acts on values nobody saw whole.
    # A run card that saves the draft first accepts it, so a cut review leaves that card to the Studio as well.
    clipped = [
        action
        for action in result["user_actions"]
        if _acted_on(action) != acted_on_before[action.get("item_id")]
        or (review_cut and (action.get("facts") or {}).get("saves_draft"))
    ]
    for action in clipped:
        action["allowed_decisions"] = []
        action["reason"] = _TOO_LARGE_REASON
    if clipped:
        result["next"] = _TOO_LARGE_NEXT
    if size() > SUMMARY_LIMIT_CHARS:
        # A card stripped of its prompt and options cannot be answered from here, so only the Studio is offered.
        if any(
            action.get("allowed_decisions") or action.get("kind") == "question" for action in result["user_actions"]
        ):
            result["next"] = _TOO_LARGE_NEXT
        result["user_actions"] = [
            {key: action.get(key) for key in ("item_id", "kind", "expires_at") if key in action}
            | {"allowed_decisions": [], "cut": True}
            for action in result["user_actions"]
        ]
    actions = result["user_actions"]
    while size() > SUMMARY_LIMIT_CHARS and actions:
        actions.pop()
        result["user_actions_omitted"] = result.get("user_actions_omitted", 0) + 1
    return result


def _offer_saved_logins(result: dict[str, Any], saved_logins: list[dict[str, str | None]], *, fit: bool) -> None:
    # A decision re-reads the card without this list, so it only gets the room left once the card itself fits.
    for action in result["user_actions"]:
        facts = action.get("facts")
        if action.get("kind") != "login_needed" or facts is None:
            continue
        for count in range(min(len(saved_logins), _MAX_SAVED_LOGINS), -1, -1):
            facts["saved_logins"] = saved_logins[:count]
            facts["saved_logins_more"] = len(saved_logins) > count
            if not fit or len(json.dumps(result, default=str)) <= SUMMARY_LIMIT_CHARS:
                break
        else:
            del facts["saved_logins"], facts["saved_logins_more"]


def _offer_last_user_message(result: dict[str, Any], message: str | None, *, fit: bool) -> None:
    # The latest message is context, so like the saved-login list it only gets the room left once the rest fits.
    for limit in (_MAX_USER_MESSAGE_CHARS, 200, 40, 0):
        result["last_user_message"] = _clip(message, limit)
        if not fit or len(json.dumps(result, default=str)) <= SUMMARY_LIMIT_CHARS:
            return
    # Left off like the list, so a message with no room never reads as no message.
    del result["last_user_message"]


def _last_user_message(history: dict[str, Any], rows: list[dict[str, Any]]) -> str | None:
    # Ember reads a steer the user sends into a running turn after that turn's opening message.
    steers = (history.get("in_flight_turn") or {}).get("steer_messages") or []
    for row in reversed(rows):
        if steers:
            break
        if row.get("sender") == "user":
            return row.get("content")
        steers = (row.get("narrative_payload") or {}).get("steerMessages") or []
    texts = [item.get("text") for item in steers if isinstance(item, dict)]
    return texts[-1] if texts else None


def map_turn_state(
    history: dict[str, Any],
    *,
    now: datetime,
    since: str | None = None,
    login_resolved: bool = False,
    fit: bool = True,
    saved_logins: list[dict[str, str | None]] | None = None,
) -> dict[str, Any]:
    """Turn one chat-history response into the turn state every Ember tool returns."""
    rows = _rows(history)
    last_ai = _last_ai_row(rows)
    links = _links(history)
    in_flight = history.get("in_flight_turn")
    turn_ended = in_flight is None and bool(rows) and rows[-1].get("sender") != "user"
    user_actions = _user_actions(history, last_ai, turn_ended=turn_ended, links=links)
    state, waiting_on = _turn_state(history, rows, last_ai, bool(user_actions) and not turn_ended, login_resolved)
    if turn_ended and state == "finished" and user_actions:
        state, waiting_on = "waiting_for_user", "you"
    # Created triggers stay on the finished row forever, so they never make the turn wait or change next.
    triggers = _trigger_actions(last_ai, links["studio"]) if turn_ended else []
    if in_flight is not None:
        narrative = in_flight.get("narrative") or {}
        turn_id = in_flight.get("turn_id")
    else:
        narrative = (last_ai or {}).get("narrative_payload") or {}
        turn_id = (last_ai or {}).get("turn_id")
    started = _parse_time(narrative.get("startedAt"))
    ended = _parse_time(narrative.get("endedAt")) if in_flight is None else None
    progress = _progress(history, narrative, since)
    proposal = _proposal(history, rows)
    # A run that first saves the draft accepts it, so it is approved here only beside that draft's whole review.
    for action in user_actions:
        facts = action.get("facts") or {}
        if facts.get("saves_draft") and (
            not facts.get("draft_proposal_id") or facts["draft_proposal_id"] != (proposal or {}).get("proposal_id")
        ):
            action["allowed_decisions"] = ["decline"]
            action["reason"] = _DRAFT_UNREVIEWED_REASON
    reply = _clip((last_ai or {}).get("content"), _MAX_REPLY_CHARS) if in_flight is None else None
    error = None
    if state == "failed":
        error = {
            "kind": ((last_ai or {}).get("turn_outcome") or {}).get("terminal_reason") or "error",
            "message": _clip(narrative.get("terminalMessage") or reply, 500),
        }
    result = {
        "ember_chat_id": chat_id_of(history),
        "workflow_id": history.get("workflow_permanent_id"),
        "turn_id": turn_id,
        "state": state,
        "waiting_on": waiting_on,
        "elapsed_seconds": int(((ended or now) - started).total_seconds()) if started else None,
        "progress": progress,
        "user_actions": user_actions + triggers,
        "delivered_steer_ids": _delivered_steer_ids(history),
        "reply": reply,
        "error": error,
        "proposal": proposal,
        "runs": _runs(history, progress),
        "links": links,
        "next": _next(state, waiting_on, user_actions, proposal, progress["cursor"]),
    }
    if fit:
        result = _fit(result)
    # The Studio or another client can start or steer a turn, so the latest message says what Ember was asked.
    _offer_last_user_message(result, _last_user_message(history, rows), fit=fit)
    if saved_logins is not None:
        _offer_saved_logins(result, saved_logins, fit=fit)
    return result


def _too_large_refusal(history: dict[str, Any], item_id: str) -> str | None:
    """Why a card cannot be acted on here: fitting cut it down, dropped it or clipped its run inputs."""
    now = datetime.now(UTC)
    offered = {action.get("item_id"): action for action in map_turn_state(history, now=now, fit=False)["user_actions"]}
    if item_id not in offered:
        return None
    decisions = offered[item_id].get("allowed_decisions")
    shown = map_turn_state(history, now=now)["user_actions"]
    if any(
        action.get("item_id") == item_id and "prompt" in action and action.get("allowed_decisions") == decisions
        for action in shown
    ):
        return None
    return (
        f"{item_id} was too large to show in full here, so it can't be answered through MCP; the user answers it in "
        f"the Studio at {_links(history)['studio']}. Nothing was sent."
    )


def _live_browser_session_id(history: dict[str, Any]) -> str | None:
    narrative = (history.get("in_flight_turn") or {}).get("narrative") or {}
    browser_session_id = narrative.get("browserSessionId")
    return browser_session_id if isinstance(browser_session_id, str) else None


def _latest_screenshot_id(history: dict[str, Any]) -> str | None:
    narrative = (history.get("in_flight_turn") or {}).get("narrative")
    if narrative is None:
        narrative = (_last_ai_row(_rows(history)) or {}).get("narrative_payload") or {}
    screenshots = [shot for shot in narrative.get("screenshots") or [] if isinstance(shot, dict)]
    artifact_id = screenshots[-1].get("artifactId") if screenshots else None
    return artifact_id if isinstance(artifact_id, str) else None


_STEER_NEXT = {
    "pending": "The running turn holds the message and reads it next; skyvern_ember_check with "
    "ember_chat_id={chat_id!r} lists its steer_id in delivered_steer_ids once it has.",
    "unconfirmed": "The send got no answer, so Ember may already hold the message; skyvern_ember_check with "
    "ember_chat_id={chat_id!r} lists its steer_id in delivered_steer_ids if Ember reads it.",
    "not_delivered": "The turn ended before Ember read the message, so Ember does not have it.",
}
_OUT_OF_TIME = (
    "The call's wait_seconds ran out before a turn could start, so nothing was sent to Ember. Call again with a "
    "larger wait_seconds."
)


def _created_note(workflow_permanent_id: str) -> str:
    return (
        f" Workflow {workflow_permanent_id} was already created for this message; call again with "
        f"workflow_id={workflow_permanent_id}."
    )


def _setup_timeout(call_deadline: float) -> float:
    remaining = _remaining(call_deadline)
    if remaining < 1.0:
        raise EmberError(ErrorCode.RETRY_LATER, _OUT_OF_TIME)
    return min(READ_TIMEOUT_SECONDS, remaining)


def _poll_timeout(deadline: float) -> float:
    return min(READ_TIMEOUT_SECONDS, _remaining(deadline) + 2.0)


async def _turn_state_with_links(
    history: dict[str, Any], *, since: str | None, deadline: float, login_resolved: bool = False
) -> dict[str, Any]:
    timeout = _poll_timeout(deadline)
    saved_logins = (
        await list_saved_logins(_MAX_SAVED_LOGINS + 1, timeout=timeout)
        if history.get("pending_credential_requests")
        else None
    )
    state = map_turn_state(
        history, now=datetime.now(UTC), since=since, login_resolved=login_resolved, saved_logins=saved_logins
    )
    if browser_session_id := _live_browser_session_id(history):
        session = await get_browser_session(browser_session_id, timeout=timeout)
        state["progress"]["live_browser_url"] = (session or {}).get("app_url")
    if artifact_id := _latest_screenshot_id(history):
        state["progress"]["screenshot_url"] = await get_artifact_url(artifact_id, timeout=timeout)
    return state


def _delivered_steer_ids(history: dict[str, Any]) -> list[str]:
    steers = list((history.get("in_flight_turn") or {}).get("steer_messages") or [])
    # A turn that ended before this read keeps its delivered steers on its AI row.
    for row in _rows(history):
        if row.get("sender") == "ai":
            steers += (row.get("narrative_payload") or {}).get("steerMessages") or []
    return [item["steer_id"] for item in steers if isinstance(item, dict) and isinstance(item.get("steer_id"), str)]


def _steer_delivered(history: dict[str, Any], steer_id: str) -> bool:
    return steer_id in _delivered_steer_ids(history)


async def _watch(
    chat_id: str,
    history: dict[str, Any],
    *,
    deadline: float,
    steer_id: str | None = None,
    login_resolved: bool = False,
) -> dict[str, Any]:
    while _remaining(deadline) > 0:
        state = map_turn_state(history, now=datetime.now(UTC), login_resolved=login_resolved)["state"]
        if state != "working" or (steer_id is not None and _steer_delivered(history, steer_id)):
            break
        await asyncio.sleep(min(POLL_SECONDS, _remaining(deadline)))
        try:
            history = await get_history(chat_id=chat_id, timeout=_poll_timeout(deadline))
        except HistoryReadTimeout:
            break
    return history


def _unread_after_write(chat_id: str, next_text: str | None = None) -> dict[str, Any]:
    check = (
        "The request reached Ember, but reading the chat afterwards failed; skyvern_ember_check with "
        f"ember_chat_id={chat_id!r} reads it."
    )
    return {"ember_chat_id": chat_id, "state": "unknown", "next": f"{next_text} {check}" if next_text else check}


def _without_volatile_keys(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in workflow.items()
        if not key.startswith("_") and key not in _VOLATILE_WORKFLOW_KEYS
    }


async def _proposal_detail(history: dict[str, Any], mode: Literal["diff", "yaml"], *, timeout: float) -> str:
    proposed = history.get("proposed_workflow")
    if not isinstance(proposed, dict):
        return "There is no proposal to show."
    try:
        # secret_encryption needs `cryptography`, which only the local and server extras install.
        from skyvern.forge.sdk.workflow.private_settings import strip_private_workflow_settings  # noqa: PLC0415
        from skyvern.forge.sdk.workflow.secret_encryption import mask_workflow_definition_secrets  # noqa: PLC0415
    except ImportError:
        return "Showing the diff or YAML needs the Skyvern server extra; open links.studio to review it."

    def masked_yaml(workflow: dict[str, Any], keys: list[str]) -> str:
        data = {key: deepcopy(workflow[key]) for key in keys if key in workflow}
        strip_private_workflow_settings(data)
        if isinstance(data.get("workflow_definition"), dict):
            mask_workflow_definition_secrets(data["workflow_definition"])
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    keys = list(_without_volatile_keys(proposed))
    proposed_yaml = masked_yaml(proposed, keys)
    if mode == "yaml":
        return _clip(proposed_yaml, DETAIL_LIMIT_CHARS) or ""
    saved = await get_workflow_by_id(history["workflow_permanent_id"], timeout=timeout)
    diff = "\n".join(
        difflib.unified_diff(
            masked_yaml(saved, keys).splitlines(),
            proposed_yaml.splitlines(),
            "saved",
            "proposal",
            lineterm="",
        )
    )
    return _clip(diff or "The proposal matches the saved workflow.", DETAIL_LIMIT_CHARS) or ""


def _transcript(rows: list[dict[str, Any]], count: int, before_message_id: str | None) -> list[dict[str, Any]]:
    if before_message_id is not None:
        ids = [row.get("ember_chat_message_id") or row.get("workflow_copilot_chat_message_id") for row in rows]
        rows = rows[: ids.index(before_message_id)] if before_message_id in ids else []
    transcript = [
        {
            "message_id": row.get("ember_chat_message_id") or row.get("workflow_copilot_chat_message_id"),
            "sender": "ember" if row.get("sender") == "ai" else row.get("sender"),
            "text": row.get("content"),
            "at": row.get("created_at"),
            "turn_id": row.get("turn_id"),
        }
        for row in rows[-count:]
    ]
    used = 0
    for index, message in enumerate(transcript):
        used += len(message["text"] or "")
        if used > DETAIL_LIMIT_CHARS:
            omitted = len(transcript) - index
            return transcript[:index] + [{"omitted": f"{omitted} newer messages omitted to stay under the size cap"}]
    return transcript


async def _workflow_request(call: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    try:
        return await call
    except RuntimeError as exc:
        # _workflow_http raises this for an HTTP error from the workflow read or create, before any turn exists.
        raise EmberError(
            ErrorCode.API_ERROR, f"Skyvern refused the workflow request ({exc}); nothing was sent."
        ) from exc


def _read_attachment(raw_path: str) -> FilePayload:
    try:
        root = Path.cwd().resolve()
    except OSError as exc:
        raise EmberError(ErrorCode.INVALID_INPUT, f"This MCP server's working directory is unreadable: {exc}") from exc
    # A server started in the home folder or above it would otherwise let a prompt upload any file the user owns.
    if Path.home().resolve().is_relative_to(root):
        raise EmberError(
            ErrorCode.INVALID_INPUT,
            f"Cannot attach files: this MCP server's working directory is {root}. Start it in a project folder.",
        )
    try:
        path = Path(raw_path).expanduser()
        if path.is_symlink():
            raise EmberError(ErrorCode.INVALID_INPUT, f"{path} is a symlink; attach the file it points to.")
        resolved = path.resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        raise EmberError(ErrorCode.INVALID_INPUT, f"Cannot read {raw_path!r}: {exc}") from exc
    if resolved.is_relative_to(root) and any(part.startswith(".") for part in resolved.relative_to(root).parts):
        raise EmberError(ErrorCode.INVALID_INPUT, f"{path} is hidden or inside a hidden folder; it is not attached.")
    try:
        return read_local_file_under(str(path), [root], MAX_ATTACHED_FILE_BYTES)
    except FileNotFoundError as exc:
        raise EmberError(ErrorCode.INVALID_INPUT, f"File not found: {path}") from exc
    except PermissionError as exc:
        raise EmberError(ErrorCode.INVALID_INPUT, f"Cannot attach {path}: {exc}. Attach files under {root}.") from exc
    except ValueError as exc:
        raise EmberError(ErrorCode.INVALID_INPUT, f"{path.name} is larger than 30 MB.") from exc
    except OSError as exc:
        raise EmberError(ErrorCode.INVALID_INPUT, f"Cannot read {path}: {exc.strerror or exc}") from exc


async def _upload_files(payloads: list[FilePayload], call_deadline: float) -> list[str]:
    # All uploads share what the call has left after the turn start's share, so a slow file cannot reset it.
    upload_deadline = call_deadline - TURN_START_MIN_SECONDS
    file_ids = []
    for payload in payloads:
        if _remaining(upload_deadline) < 1.0:
            raise EmberError(ErrorCode.RETRY_LATER, _OUT_OF_TIME)
        file_ids.append(await upload_file(payload["name"], payload["buffer"], timeout=_remaining(upload_deadline)))
    return file_ids


async def _start_browser(workflow_permanent_id: str, call_deadline: float) -> str | None:
    budget = _remaining(call_deadline) - TURN_START_MIN_SECONDS
    if budget <= 0:
        return None
    browser_deadline = time.monotonic() + budget
    browser_session_id = await get_debug_session_browser_id(workflow_permanent_id, timeout=budget)
    while browser_session_id is not None and _remaining(browser_deadline) > 0:
        session = await get_browser_session(browser_session_id, timeout=_remaining(browser_deadline))
        if session is None or session.get("started_at") or session.get("status") not in (None, "created"):
            break
        await asyncio.sleep(min(1.0, _remaining(browser_deadline)))
    # Passed either way: Ember falls back to its own browser when this one is not usable.
    return browser_session_id


async def _recover_chat(workflow_permanent_id: str, cancel_token: str, deadline: float) -> dict[str, Any] | None:
    while True:
        try:
            history = await get_history(
                workflow_permanent_id=workflow_permanent_id,
                request_cancel_token=cancel_token,
                timeout=_poll_timeout(deadline),
            )
        except (EmberError, httpx.HTTPError):
            return None
        if history.get("request_turn_id") and chat_id_of(history):
            return history
        if _remaining(deadline) <= 0:
            return None
        await asyncio.sleep(min(POLL_SECONDS, _remaining(deadline)))


async def _find_live_chat(
    workflow_permanent_id: str, chosen_history: dict[str, Any] | None, call_deadline: float
) -> tuple[list[dict[str, Any]] | None, tuple[str, dict[str, Any]] | None]:
    try:
        chats = await list_all_chats(workflow_permanent_id, timeout=lambda: _setup_timeout(call_deadline))
    except ChatScanCapped:
        # Too many chats to scan: only a running turn in the chosen chat can be acted on, and chats comes back None.
        chosen_chat_id = chat_id_of(chosen_history) if chosen_history else None
        if chosen_history is None or chosen_chat_id is None or chosen_history.get("in_flight_turn") is None:
            raise
        return None, (chosen_chat_id, chosen_history)
    for chat in chats:
        chat_id = chat_id_of(chat)
        if not chat.get("in_flight") or chat_id is None:
            continue
        # The summary's in_flight stays true for a dead turn until a chat-history read reconciles it.
        history = await get_history(chat_id=chat_id, timeout=_setup_timeout(call_deadline))
        if history.get("in_flight_turn") is not None:
            return chats, (chat_id, history)
    return chats, None


async def skyvern_ember_send(
    message: Annotated[
        str | None,
        Field(
            description=(
                "The user's request. Ember reads it as the user's own chat message, as if typed in the Studio, and "
                "asks the user about anything it leaves open. Required, except with action='test_end_to_end'."
            )
        ),
    ] = None,
    workflow_id: Annotated[
        str | None, Field(description="The workflow's wpid_ id. Leave out to create a blank workflow.")
    ] = None,
    title: Annotated[
        str | None, Field(description="Title for a new blank workflow; only used with no workflow.")
    ] = None,
    ember_chat_id: Annotated[
        str | None,
        Field(description="The chat to continue (runs report it as copilot_session_id). Default: the newest chat."),
    ] = None,
    new_chat: Annotated[bool, Field(description="Start a new chat on the workflow instead of continuing one.")] = False,
    if_turn_running: Annotated[
        Literal["refuse", "steer"],
        Field(description="When a turn is running: 'refuse' (default) or 'steer' the message into that same chat."),
    ] = "refuse",
    action: Annotated[
        Literal["message", "test_end_to_end", "diagnose_run", "regenerate_block"],
        Field(description="'message', or a Studio action: test the proposal, diagnose a run, regenerate a block."),
    ] = "message",
    workflow_run_id: Annotated[
        str | None, Field(description="The wr_ run the user is talking about; required for diagnose_run.")
    ] = None,
    block_label: Annotated[
        str | None, Field(description="The block the user means; required for regenerate_block.")
    ] = None,
    connected_account_id: Annotated[
        str | None,
        Field(
            description=(
                "Only an account Ember offered in a choose_account item. It is sent on its own as the user's "
                "pick, so leave message out."
            )
        ),
    ] = None,
    file_paths: Annotated[
        list[str] | None,
        Field(
            description=(
                "Local files to attach (stdio only; under the server's working directory, no hidden files or "
                "symlinks; at most 20, 30 MB each)."
            )
        ),
    ] = None,
    wait_seconds: Annotated[
        int,
        Field(
            description=(
                "Deadline for the whole call, in seconds; reading back the result can add up to 6 seconds. Below "
                "15, the call still takes up to 15 seconds to start the turn; attached files need more."
            ),
            ge=0,
            le=MAX_WAIT_SECONDS,
        ),
    ] = 30,
) -> dict[str, Any]:
    """Ember (the new name for Skyvern's Workflow Copilot; the app may still say Copilot) is Skyvern's
    workflow-building agent: it opens its own browser, writes blocks, test-runs them and proposes a draft.
    Use this tool only when the user names Ember or Copilot, to ask Ember to build, change, test or fix a workflow.

    It starts an Ember turn, which takes minutes, and returns the turn state; keep watching with
    skyvern_ember_check and its cursor. While a turn is running, it refuses (TURN_IN_PROGRESS) unless
    if_turn_running='steer' on that chat, which adds the message to the running turn. It never answers Ember's
    questions or approvals. If a call was interrupted, call skyvern_ember_check or skyvern_ember_list before
    sending again, so the message is not sent twice. state 'unknown' means the chat could not be read after
    sending. A steered message's steer_status is delivered, pending (the running turn has not read it yet),
    unconfirmed (the send got no answer, so it may have landed) or not_delivered (the turn ended first); its
    steer_id shows up in delivered_steer_ids once Ember reads it.
    """
    action_name = "skyvern_ember_send"
    with Timer() as timer:
        deadline = time.monotonic() + wait_seconds
        # Every step draws on this one budget; starting a turn needs a minimum even when wait_seconds is lower.
        call_deadline = time.monotonic() + max(wait_seconds, TURN_START_MIN_SECONDS)
        # The server reads a pick from a message that is exactly the account id, as the Studio sends it; a pick
        # answering a Test end-to-end picker resumes that Test only then.
        text = connected_account_id or message or (TEST_END_TO_END_MESSAGE if action == "test_end_to_end" else None)
        invalid = None
        if not text or not text.strip():
            invalid = "message is required."
        elif action == "diagnose_run" and not workflow_run_id:
            invalid = "action='diagnose_run' needs workflow_run_id."
        elif action == "regenerate_block" and not block_label:
            invalid = "action='regenerate_block' needs block_label."
        elif connected_account_id and message and message != connected_account_id:
            invalid = (
                "connected_account_id is sent on its own as the user's pick; leave message out and send anything "
                "else after Ember goes on."
            )
        elif ember_chat_id and new_chat:
            invalid = "Give ember_chat_id or new_chat=true, not both."
        elif file_paths and not is_stdio_local_file_access_enabled():
            invalid = "Attaching files works only on a local stdio server; attach them in the Studio instead."
        elif file_paths and len(file_paths) > MAX_ATTACHED_FILES:
            invalid = f"At most {MAX_ATTACHED_FILES} files can be attached."
        elif file_paths and wait_seconds <= TURN_START_MIN_SECONDS:
            invalid = (
                f"Attached files need wait_seconds above {TURN_START_MIN_SECONDS:g}, since that much is kept for "
                "starting the turn."
            )
        if invalid or text is None:
            return _error(action_name, ErrorCode.INVALID_INPUT, invalid or "message is required.", timer)
        created_workflow = False
        try:
            attachments = [_read_attachment(raw_path) for raw_path in file_paths or []]
            target_history: dict[str, Any] | None = None
            if ember_chat_id:
                target_history = await get_history(chat_id=ember_chat_id, timeout=_setup_timeout(call_deadline))
                workflow_permanent_id = target_history["workflow_permanent_id"]
                if workflow_id and workflow_id != workflow_permanent_id:
                    raise EmberError(
                        ErrorCode.INVALID_INPUT,
                        f"Chat {ember_chat_id} is on workflow {workflow_permanent_id}, not {workflow_id}. Nothing was "
                        "sent; give the chat, or the workflow, that the user meant.",
                    )
                workflow = await _workflow_request(
                    get_workflow_by_id(workflow_permanent_id, timeout=_setup_timeout(call_deadline))
                )
                version_id = workflow["workflow_id"]
            elif workflow_id:
                workflow_permanent_id = workflow_id
                workflow = await _workflow_request(
                    get_workflow_by_id(workflow_permanent_id, timeout=_setup_timeout(call_deadline))
                )
                version_id = workflow["workflow_id"]
            else:
                created = await _workflow_request(
                    create_workflow_raw(
                        json_definition={**_STUDIO_BLANK_WORKFLOW, "title": title or _STUDIO_BLANK_WORKFLOW["title"]},
                        yaml_definition=None,
                        folder_id=None,
                        timeout=_setup_timeout(call_deadline),
                    )
                )
                workflow_permanent_id, version_id = created["workflow_permanent_id"], created["workflow_id"]
                created_workflow = True
            timer.mark("workflow")

            target_chat_id = ember_chat_id
            if not created_workflow:
                chats, live = await _find_live_chat(workflow_permanent_id, target_history, call_deadline)
                if target_chat_id is None and not new_chat and chats:
                    target_chat_id = chat_id_of(chats[0])
                if live is not None:
                    live_chat_id, live_history = live
                    live_state = await _turn_state_with_links(live_history, since=None, deadline=call_deadline)
                    if if_turn_running != "steer" or new_chat or live_chat_id != target_chat_id:
                        return _error(
                            action_name,
                            ErrorCode.TURN_IN_PROGRESS,
                            f"Ember is already running a turn on this workflow in chat {live_chat_id}.",
                            timer,
                            data=live_state,
                        )
                    cancel_token = (live_history.get("in_flight_turn") or {}).get("cancel_token")
                    if not cancel_token:
                        return _error(
                            action_name,
                            ErrorCode.TURN_CONTROL_DENIED,
                            "Only the person who started this Ember turn can add messages to it.",
                            timer,
                            data=live_state,
                        )
                    if attachments:
                        return _error(
                            action_name,
                            ErrorCode.INVALID_INPUT,
                            "Files can't be added to a running Ember turn. Send them once it ends.",
                            timer,
                            data=live_state,
                        )
                    if action != "message":
                        # A steer carries only text, so the run, block or test the action names would be lost.
                        return _error(
                            action_name,
                            ErrorCode.INVALID_INPUT,
                            f"A running Ember turn takes only a plain message. Send action={action!r} once it ends.",
                            timer,
                            data=live_state,
                        )
                    if live_state["state"] == "waiting_for_user":
                        # The route would queue it, but the turn reads nothing new until the user answers.
                        return _error(
                            action_name,
                            ErrorCode.TURN_IN_PROGRESS,
                            "Ember is waiting for the user to answer it and won't read a new message until they do. "
                            "Show the user what Ember is asking and pass their answer to skyvern_ember_answer or "
                            "skyvern_ember_decide, then send this.",
                            timer,
                            data=live_state,
                        )
                    steer_id = uuid.uuid4().hex
                    unanswered = False
                    try:
                        delivered = await steer(
                            chat_id=live_chat_id,
                            cancel_token=cancel_token,
                            steer_id=steer_id,
                            message=text,
                            timeout=_setup_timeout(call_deadline),
                        )
                    except httpx.HTTPError as exc:
                        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                            raise
                        # The server may have taken the steer before the answer was lost; its id shows if it landed.
                        delivered, unanswered = {}, True
                    if delivered is not None:
                        try:
                            history = await get_history(chat_id=live_chat_id, timeout=_poll_timeout(deadline))
                            history = await _watch(live_chat_id, history, deadline=deadline, steer_id=steer_id)
                            state = await _turn_state_with_links(history, since=None, deadline=deadline)
                            # A running turn, paused for the user or not, still holds an unread message.
                            if _steer_delivered(history, steer_id):
                                steer_status = "delivered"
                            elif unanswered:
                                steer_status = "unconfirmed"
                            elif history.get("in_flight_turn") is not None:
                                steer_status = "pending"
                            else:
                                steer_status = "not_delivered"
                        except (EmberError, httpx.HTTPError):
                            state = _unread_after_write(live_chat_id) | {"workflow_id": workflow_permanent_id}
                            steer_status = "unconfirmed" if unanswered else "pending"
                        state |= {
                            "delivered_as": "steer",
                            "steer_id": steer_id,
                            "steer_status": steer_status,
                            "created_workflow": False,
                        }
                        if steer_status != "delivered":
                            state["next"] = _STEER_NEXT[steer_status].format(chat_id=live_chat_id)
                        return make_result(action_name, data=state, timing_ms=timer.timing_ms)
                    if chats is None:
                        raise EmberError(
                            ErrorCode.API_ERROR,
                            "The running turn ended before the message reached it. This workflow has too many Ember "
                            "chats to check for another running turn, so no new turn was started; send it from the "
                            "Studio.",
                        )
                    target_history = None
                timer.mark("concurrency")
            if target_chat_id is not None and target_history is None:
                target_history = await get_history(
                    chat_id=target_chat_id, message_limit=1, timeout=_setup_timeout(call_deadline)
                )
            if connected_account_id and connected_account_id not in _offered_accounts(target_history):
                raise EmberError(
                    ErrorCode.INVALID_INPUT,
                    "Ember did not offer that account in this chat. Pass one of the choose_account item's options "
                    "as connected_account_id.",
                )

            # Files go first: the browser is optional, so its wait gets whatever the uploads leave.
            attached_file_ids = await _upload_files(attachments, call_deadline)
            browser_session_id = await _start_browser(workflow_permanent_id, call_deadline)
            timer.mark("browser")

            cancel_token = str(uuid.uuid4())
            body: dict[str, Any] = {
                "workflow_id": version_id,
                "workflow_permanent_id": workflow_permanent_id,
                "ember_chat_id": target_chat_id,
                "workflow_run_id": workflow_run_id,
                "browser_session_id": browser_session_id,
                "message": text,
                "attached_file_ids": attached_file_ids,
                "workflow_yaml": "",
                "mode": "build",
                "doc_editor_enabled": True,
                "cancel_token": cancel_token,
                "target_block_label": block_label if action == "regenerate_block" else None,
                "selected_block_label": block_label if action != "regenerate_block" else None,
                "product_action": action if action in ("test_end_to_end", "diagnose_run") else None,
                "keep_pending_proposal": bool(target_history and target_history.get("proposed_workflow")),
                "credential_recovery_token": recovery_token(),
                "supports_credential_pause": True,
                "supports_credential_generation": True,
                "supports_credential_pause_recovery": True,
                "supports_question_tool": True,
                "supports_account_group_card": True,
                "supports_single_run_card": True,
                "supports_credential_delete_card": True,
                "supports_connection_card": True,
                "supports_demonstration_offer": False,
                "client_detached": True,
                "refuse_if_turn_running": True,
            }
            if connected_account_id:
                body["selected_connected_account_id"] = connected_account_id
                body["idempotency_key"] = str(uuid.uuid4())

            # A turn posted after the budget ran out would land after the caller stopped waiting for its id.
            if _remaining(call_deadline) < 1.0:
                raise EmberError(ErrorCode.RETRY_LATER, _OUT_OF_TIME)
            stream_seconds = _remaining(call_deadline)
            try:
                async with asyncio.timeout(stream_seconds):
                    frame = await post_chat(body, timeout=stream_seconds)
            except TimeoutError:
                frame = None
            timer.mark("turn_start")
            recovered: dict[str, Any] | None = None
            if frame is None:
                recovered = await _recover_chat(workflow_permanent_id, cancel_token, call_deadline)
                if recovered is None:
                    return make_result(
                        action_name,
                        data={
                            "ember_chat_id": None,
                            "workflow_id": workflow_permanent_id,
                            "state": "submitted_unconfirmed",
                            "delivered_as": "new_turn",
                            "created_workflow": created_workflow,
                            "next": (
                                "The message may or may not have reached Ember; skyvern_ember_check with "
                                f"workflow_id={workflow_permanent_id!r} shows whether it did."
                            ),
                        },
                        timing_ms=timer.timing_ms,
                    )
                chat_id = chat_id_of(recovered) or ""
            else:
                chat_id = chat_id_of(frame) or target_chat_id or ""
            try:
                if recovered is not None:
                    history = recovered
                else:
                    history = await get_history(chat_id=chat_id, timeout=_poll_timeout(call_deadline))
                if not created_workflow and target_chat_id is not None:
                    history = await _watch(chat_id, history, deadline=deadline)
                state = await _turn_state_with_links(history, since=None, deadline=deadline)
            except (EmberError, httpx.HTTPError):
                state = _unread_after_write(chat_id) | {"workflow_id": workflow_permanent_id}
            state |= {"delivered_as": "new_turn", "created_workflow": created_workflow}
            return make_result(action_name, data=state, timing_ms=timer.timing_ms)
        except EmberError as exc:
            if created_workflow:
                exc = EmberError(
                    exc.code, exc.message + _created_note(workflow_permanent_id), retry_after=exc.retry_after
                )
            return _ember_error(action_name, exc, timer)
        except NotFoundError:
            return _error(action_name, ErrorCode.WORKFLOW_NOT_FOUND, f"Workflow {workflow_id!r} not found.", timer)
        except httpx.HTTPError:
            # Only the workflow read or create and a steer that never connected get here; chat-post and a sent
            # steer handle their own failures.
            note = (
                _created_note(workflow_permanent_id)
                if created_workflow
                else " If this call was creating a workflow, check skyvern_ember_list or the Studio before calling "
                "again, since a blank workflow may have been created."
            )
            return _error(
                action_name,
                ErrorCode.RETRY_LATER,
                "Skyvern did not answer in time before the turn started, so nothing was sent to Ember." + note,
                timer,
            )


async def skyvern_ember_check(
    ember_chat_id: Annotated[str | None, Field(description="The chat to check. Give this or workflow_id.")] = None,
    workflow_id: Annotated[
        str | None, Field(description="A wpid_ id; checks that workflow's newest chat. Give this or ember_chat_id.")
    ] = None,
    wait_seconds: Annotated[
        int,
        Field(
            description=(
                "Deadline for the whole call, in seconds; reading back the result can add up to 6 seconds. The wait "
                "for the turn to pause or end stops early enough to leave 10 seconds for each transcript or "
                "proposal read, and the call takes at least that long."
            ),
            ge=0,
            le=MAX_WAIT_SECONDS,
        ),
    ] = 30,
    since: Annotated[str | None, Field(description="progress.cursor from the last result; only newer lines.")] = None,
    transcript_messages: Annotated[
        int, Field(description="Also return this many recent chat messages.", ge=0, le=30)
    ] = 0,
    before_message_id: Annotated[
        str | None, Field(description="Page the transcript back from this message_id.")
    ] = None,
    proposal_detail: Annotated[
        Literal["summary", "diff", "yaml"],
        Field(description="'diff' or 'yaml' also returns Ember's proposed workflow, with secrets masked."),
    ] = "summary",
) -> dict[str, Any]:
    """See what Ember (the new name for Skyvern's Workflow Copilot; the app may still say Copilot) is doing or did
    in a chat: the latest user message, progress, questions and approvals waiting on the user, the final reply, and
    the proposal awaiting review. Use it for "what is Ember/Copilot doing" and to keep watching a turn that
    skyvern_ember_send started.

    With wait_seconds it polls until the turn pauses for the user or ends, and returns at once when nothing is
    running. Pass since=progress.cursor from the last result to get only new progress lines. Like the Studio's own
    reads, each call counts as the user's client checking in. proposal.review says whether changes is the whole
    review ('shown'), shortened to fit ('cut') or not available ('missing'); a user action with cut: true was
    shortened to fit, and the Studio at links.studio shows it whole. delivered_steer_ids lists the steer_id of
    each steered message Ember has read.
    """
    action_name = "skyvern_ember_check"
    with Timer() as timer:
        if (ember_chat_id is None) == (workflow_id is None):
            return _error(
                action_name, ErrorCode.INVALID_INPUT, "Give exactly one of ember_chat_id or workflow_id.", timer
            )
        follow_up_reads = int(bool(transcript_messages or before_message_id)) + int(proposal_detail != "summary")
        # The wait stops early enough that each read after it still gets a full read timeout inside the call.
        call_deadline = time.monotonic() + max(wait_seconds, READ_TIMEOUT_SECONDS * (1 + follow_up_reads))
        deadline = min(time.monotonic() + wait_seconds, call_deadline - READ_TIMEOUT_SECONDS * follow_up_reads)
        try:
            history = await get_history(
                chat_id=ember_chat_id, workflow_permanent_id=workflow_id, timeout=READ_TIMEOUT_SECONDS
            )
            chat_id = chat_id_of(history)
            if chat_id is None:
                state = map_turn_state(history, now=datetime.now(UTC))
                state["next"] = "This workflow has no Ember chat yet. Call skyvern_ember_send to start one."
                return make_result(action_name, data=state, timing_ms=timer.timing_ms)
            history = await _watch(chat_id, history, deadline=deadline)
            if transcript_messages or before_message_id:
                history = await get_history(chat_id=chat_id, message_limit=None, timeout=READ_TIMEOUT_SECONDS)
            state = await _turn_state_with_links(history, since=since, deadline=deadline)
            if transcript_messages or before_message_id:
                state["transcript"] = _transcript(_rows(history), transcript_messages or 10, before_message_id)
            if proposal_detail != "summary":
                state[f"proposal_{proposal_detail}"] = await _proposal_detail(
                    history, proposal_detail, timeout=READ_TIMEOUT_SECONDS
                )
        except EmberError as exc:
            return _ember_error(action_name, exc, timer)
        except NotFoundError:
            return _error(action_name, ErrorCode.WORKFLOW_NOT_FOUND, "The chat's workflow was not found.", timer)
        except (RuntimeError, httpx.HTTPError) as exc:
            return _error(action_name, ErrorCode.API_ERROR, f"Could not read the proposal's workflow ({exc}).", timer)
    return make_result(action_name, data=state, timing_ms=timer.timing_ms)


async def skyvern_ember_list(
    workflow_id: Annotated[str | None, Field(description="Only chats on this wpid_ workflow.")] = None,
    search: Annotated[
        str | None, Field(description="Search the first message of each chat, which is its title.")
    ] = None,
    page: Annotated[int, Field(description="Page number (1-based).", ge=1)] = 1,
    page_size: Annotated[int, Field(description="Chats per page.", ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """List or search Ember (the new name for Skyvern's Workflow Copilot; the app may still say Copilot) chats
    across the organization or for one workflow, newest first, with which ones are running or waiting on the user.
    """
    action_name = "skyvern_ember_list"
    with Timer() as timer:
        try:
            rows = await list_chats(
                workflow_permanent_id=workflow_id,
                search=search,
                page=page,
                page_size=page_size,
                timeout=READ_TIMEOUT_SECONDS,
            )
        except EmberError as exc:
            return _ember_error(action_name, exc, timer)
    chats = [
        {
            "ember_chat_id": chat_id_of(row),
            "workflow_id": row.get("workflow_permanent_id"),
            "workflow_title": row.get("workflow_title"),
            "title": row.get("title"),
            "modified_at": row.get("modified_at"),
            "awaiting_user_input": row.get("awaiting_user_input", False),
            "in_flight": row.get("in_flight", False),
        }
        for row in rows
    ]
    data: dict[str, Any] = {"chats": chats, "page": page, "page_size": page_size, "has_more": len(rows) == page_size}
    if not chats:
        data["note"] = f"No Ember chats found (workflow_id={workflow_id!r}, search={search!r}, page={page})."
    return make_result(action_name, data=data, timing_ms=timer.timing_ms)


async def skyvern_ember_stop(
    ember_chat_id: Annotated[str, Field(description="The chat whose running turn to stop.")],
    wait_seconds: Annotated[
        int,
        Field(
            description=(
                "Deadline for the whole call, in seconds, including the wait for the turn to show as stopped; "
                "reading back the result can add up to 6 seconds. Below 20, the call still takes up to 20 seconds "
                "to read the chat and send the stop."
            ),
            ge=0,
            le=MAX_WAIT_SECONDS,
        ),
    ] = 10,
) -> dict[str, Any]:
    """Stop the running turn of Ember (the new name for Skyvern's Workflow Copilot; the app may still say Copilot)
    in a chat. Use it only when the user asks to stop Ember, or before replacing what Ember is doing.
    """
    action_name = "skyvern_ember_stop"
    deletion_in_progress = False
    with Timer() as timer:
        deadline = time.monotonic() + wait_seconds
        try:
            history = await get_history(chat_id=ember_chat_id, timeout=READ_TIMEOUT_SECONDS)
            in_flight = history.get("in_flight_turn")
            pending_questions = [
                item
                for item in history.get("question_interactions") or []
                if isinstance(item, dict) and item.get("status") == "pending"
            ]
            if in_flight is None and not pending_questions:
                state = map_turn_state(history, now=datetime.now(UTC))
                state["next"] = "Nothing is running in this chat, so there is nothing to stop."
                return make_result(action_name, data=state, timing_ms=timer.timing_ms)
            token = (in_flight or {}).get("cancel_token") or history.get("pending_question_cancel_token")
            if not token:
                return _error(
                    action_name,
                    ErrorCode.TURN_CONTROL_DENIED,
                    "Only the person who started this Ember turn, or an Admin, can stop it.",
                    timer,
                    data=await _turn_state_with_links(history, since=None, deadline=deadline),
                )
            deletion_in_progress = any(_claimed_deletion(item) for item in pending_questions)
            await cancel(chat_id=ember_chat_id, cancel_token=token, timeout=READ_TIMEOUT_SECONDS)
            try:
                history = await get_history(chat_id=ember_chat_id, timeout=_poll_timeout(deadline))
                while _remaining(deadline) > 0 and history.get("in_flight_turn") is not None:
                    await asyncio.sleep(min(1.0, _remaining(deadline)))
                    try:
                        history = await get_history(chat_id=ember_chat_id, timeout=_poll_timeout(deadline))
                    except HistoryReadTimeout:
                        break
                state = await _turn_state_with_links(history, since=None, deadline=deadline)
            except (EmberError, httpx.HTTPError):
                state = _unread_after_write(ember_chat_id)
        except EmberError as exc:
            return _ember_error(action_name, exc, timer)
    if deletion_in_progress:
        state["deletion_in_progress"] = True
        state["next"] = (
            "Ember is stopping. The credential deletions the user already confirmed will still finish; check "
            "links.credentials afterwards."
        )
    return make_result(action_name, data=state, timing_ms=timer.timing_ms)


DEFAULT_DECISION_WAIT_SECONDS = 20
MAX_RATING_REASON_CHARS = 2000
EXPIRED_LOGIN_PICK_MESSAGE = "Use the credential {credential_id} — continue."

Decision = Literal[
    "approve",
    "decline",
    "accept",
    "reject",
    "turn_off_auto_accept",
    "use_saved_login",
    "skip",
    "generate_login",
    "sign_in_myself",
    "signed_in",
]
_PROPOSAL_DECISIONS: tuple[Decision, ...] = ("accept", "reject")
_LOGIN_ACTIONS: dict[Decision, str] = {
    "use_saved_login": "connected",
    "skip": "skip",
    "sign_in_myself": "signing_in",
    "signed_in": "signed_in",
}


class EmberQuestionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_id: str = Field(description="options[].part_id of the question.")
    choice_id: str | None = Field(default=None, description="options[].choices[].choice_id the user picked.")
    text: str | None = Field(default=None, description="The user's own words for this part.")


def _item_refusal(item_id: str) -> str | None:
    if item_id.startswith(TRIGGER_ITEM_PREFIX):
        return (
            "No MCP call can turn on a trigger Ember created. Tell the user to turn it on in the Studio's Triggers "
            "panel at links.studio."
        )
    if item_id.startswith(_SEND_ITEM_PREFIXES):
        return "Account choices go through skyvern_ember_send with connected_account_id, not this tool."
    return None


def _pending_question(history: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    for item in history.get("question_interactions") or []:
        if isinstance(item, dict) and item.get("interaction_id") == item_id:
            return item if item.get("status") == "pending" and not _claimed_deletion(item) else None
    return None


def _login_item_id(card: dict[str, Any]) -> str:
    # A turn can pause for a second login after the first is answered; each pause gets its own resume token, so a
    # digest of it keeps a late decision for the first card from landing on the second.
    token = card.get("resume_token")
    pause = f":{hashlib.sha256(token.encode()).hexdigest()[:12]}" if isinstance(token, str) else ""
    return f"{LOGIN_ITEM_PREFIX}{card.get('turn_id')}{pause}"


def _login_card(history: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    cards = history.get("pending_credential_requests") or []
    return next((card for card in cards if isinstance(card, dict) and _login_item_id(card) == item_id), None)


async def _expired(
    action: str, item_id: str, history: dict[str, Any], timer: Timer, *, deadline: float, next_text: str | None = None
) -> dict[str, Any]:
    state = await _turn_state_with_links(history, since=None, deadline=deadline)
    if next_text is not None:
        state["next"] = next_text
    return _error(
        action, ErrorCode.ITEM_EXPIRED, f"Item {item_id} is no longer waiting on the user.", timer, data=state
    )


def _not_allowed(action: str, item_id: str, decision: Decision, allowed: list[str], timer: Timer) -> dict[str, Any]:
    return _error(
        action,
        ErrorCode.INVALID_INPUT,
        f"{decision!r} is not allowed for {item_id}. Allowed decisions: {allowed}.",
        timer,
    )


def _is_proposal_id(item_id: str) -> bool:
    owner_turn_id, _, revision = item_id.rpartition(":")
    return bool(owner_turn_id) and revision.isdigit()


def _expired_pick_next(credential_id: str) -> str:
    message = EXPIRED_LOGIN_PICK_MESSAGE.format(credential_id=credential_id)
    return (
        "The login card is gone, so this login did not reach Ember's turn. If the user still wants it, call "
        f"skyvern_ember_send with message {message!r}."
    )


async def _result_after(
    action: str,
    chat_id: str,
    deadline: float,
    timer: Timer,
    extra: dict[str, Any],
    next_text: str | None = None,
    *,
    login_resolved: bool = False,
) -> dict[str, Any]:
    try:
        # The decision is already sent, so this read only reports; it gets what the call has left.
        history = await get_history(chat_id=chat_id, timeout=_poll_timeout(deadline))
        history = await _watch(chat_id, history, deadline=deadline, login_resolved=login_resolved)
        state = await _turn_state_with_links(history, since=None, deadline=deadline, login_resolved=login_resolved)
        if next_text is not None:
            state["next"] = next_text
    except (EmberError, httpx.HTTPError):
        state = _unread_after_write(chat_id, next_text)
    return make_result(action, data=state | extra, timing_ms=timer.timing_ms)


async def skyvern_ember_answer(
    ember_chat_id: Annotated[str, Field(description="The chat whose item to answer.")],
    item_id: Annotated[
        str | None, Field(description="A question's user_actions[].item_id. Give this or turn_id, not both.")
    ] = None,
    answers: Annotated[
        list[EmberQuestionAnswer] | None, Field(description="Per-part answers, as the user gave them.")
    ] = None,
    text: Annotated[str | None, Field(description="A free-text answer to the whole question.")] = None,
    skip: Annotated[bool, Field(description="The user chose not to answer the question.")] = False,
    turn_id: Annotated[
        str | None, Field(description="A finished turn to rate. Give this or item_id, not both.")
    ] = None,
    rating: Annotated[
        Literal["up", "down"] | None, Field(description="With turn_id: thumbs up or down; leave out to clear.")
    ] = None,
    reason: Annotated[
        str | None,
        Field(description="With turn_id: the user's reason for the rating.", max_length=MAX_RATING_REASON_CHARS),
    ] = None,
    wait_seconds: Annotated[
        int,
        Field(
            description=(
                "Deadline for the whole call, in seconds, including the wait for Ember to go on. Below 20, the "
                "call still takes up to 20 seconds to read the chat and send the answer."
            ),
            ge=0,
            le=MAX_WAIT_SECONDS,
        ),
    ] = DEFAULT_DECISION_WAIT_SECONDS,
) -> dict[str, Any]:
    """Pass the user's answer to a question from Ember (the new name for Skyvern's Workflow Copilot; the app may
    still say Copilot), or the user's thumbs rating of a finished turn. Use it only with the user's own words or
    choice; never answer for them. Approvals, deletions, login cards and proposals go through skyvern_ember_decide.
    Returns the turn state, like skyvern_ember_check. A rating returns {ember_chat_id, turn_id, rating}.
    """
    action_name = "skyvern_ember_answer"
    with Timer() as timer:
        deadline = time.monotonic() + wait_seconds
        invalid = None
        if (item_id is None) == (turn_id is None):
            invalid = "Give exactly one of item_id (to answer a question) or turn_id (to rate a turn)."
        elif turn_id is not None and (answers or text is not None or skip):
            invalid = "A rating takes only turn_id, rating and reason."
        elif item_id is not None and (rating is not None or reason is not None):
            invalid = "rating and reason go with turn_id, not item_id."
        elif item_id is not None and skip and (answers or text is not None):
            invalid = "Give answers or text, or skip=true, not both."
        elif item_id is not None and not skip and not answers and not text:
            invalid = "Give answers, text or skip=true."
        elif item_id is not None and item_id.startswith(LOGIN_ITEM_PREFIX):
            invalid = f"{item_id} is a login card; pass the user's choice to skyvern_ember_decide."
        elif item_id is not None and (refusal := _item_refusal(item_id)):
            invalid = refusal
        elif item_id is not None and _is_proposal_id(item_id):
            invalid = f"{item_id} is a proposal; pass the user's Accept or Reject to skyvern_ember_decide."
        if invalid:
            return _error(action_name, ErrorCode.INVALID_INPUT, invalid, timer)
        try:
            if turn_id is not None:
                response = await post_action(
                    "message-feedback",
                    {"ember_chat_id": ember_chat_id, "turn_id": turn_id, "rating": rating, "reason": reason},
                    timeout=READ_TIMEOUT_SECONDS,
                )
                feedback = (response or {}).get("feedback") or {}
                return make_result(
                    action_name,
                    data={"ember_chat_id": ember_chat_id, "turn_id": turn_id, "rating": feedback.get("rating")},
                    timing_ms=timer.timing_ms,
                )
            history = await get_history(chat_id=ember_chat_id, timeout=READ_TIMEOUT_SECONDS)
            item = _pending_question(history, str(item_id))
            if item is None:
                return await _expired(action_name, str(item_id), history, timer, deadline=deadline)
            kind = _question_action(item, None, None)["kind"]
            if kind != "question":
                return _error(
                    action_name,
                    ErrorCode.INVALID_INPUT,
                    f"{item_id} is a {kind} item; pass the user's choice to skyvern_ember_decide.",
                    timer,
                )
            if refusal := _too_large_refusal(history, str(item_id)):
                return _error(action_name, ErrorCode.INVALID_INPUT, refusal, timer)
            if answers and not skip:
                # The server resolves the whole question on one response, so an unanswered part would close unseen.
                answered = {answer.part_id for answer in answers}
                missing = [
                    str(part.get("part_id"))
                    for part in item.get("parts") or []
                    if isinstance(part, dict) and part.get("part_id") not in answered
                ]
                if missing:
                    return _error(
                        action_name,
                        ErrorCode.INVALID_INPUT,
                        f"answers must cover every part of {item_id}; these part_ids have no answer: {missing}. "
                        "A free-text answer to the whole question goes in text without answers.",
                        timer,
                    )
            detail_prompts = {
                (part.get("part_id"), choice.get("choice_id")): choice["detail_prompt"]
                for part in item.get("parts") or []
                if isinstance(part, dict)
                for choice in part.get("choices") or []
                if isinstance(choice, dict) and choice.get("detail_prompt")
            }
            for answer in answers or []:
                if not answer.choice_id and not (answer.text and answer.text.strip()):
                    return _error(
                        action_name,
                        ErrorCode.INVALID_INPUT,
                        f"The answer for part {answer.part_id} needs a choice_id or text.",
                        timer,
                    )
                prompt = detail_prompts.get((answer.part_id, answer.choice_id))
                if prompt and not (answer.text and answer.text.strip()):
                    return _error(
                        action_name,
                        ErrorCode.INVALID_INPUT,
                        f"Choice {answer.choice_id} of part {answer.part_id} needs the user's detail in text: {prompt}",
                        timer,
                    )
            record = await post_action(
                "question-response",
                {
                    "ember_chat_id": ember_chat_id,
                    "interaction_id": item_id,
                    "answers": [answer.model_dump(exclude_none=True) for answer in answers or []],
                    "text": text,
                    "skipped": skip,
                },
                timeout=READ_TIMEOUT_SECONDS,
            )
            recorded = {"status": (record or {}).get("status"), "response": (record or {}).get("response")}
            return await _result_after(action_name, ember_chat_id, deadline, timer, {"answer_recorded": recorded})
        except EmberError as exc:
            return _ember_error(action_name, exc, timer)


def _decide_refusal(
    decision: Decision,
    item_id: str | None,
    credential_ids: list[str] | None,
    site_answer: str | None,
    auto_accept: bool,
) -> str | None:
    if decision == "turn_off_auto_accept":
        extras = item_id is not None or credential_ids is not None or site_answer is not None or auto_accept
        return "turn_off_auto_accept takes only ember_chat_id and wait_seconds." if extras else None
    if item_id is None:
        return f"{decision} needs item_id."
    if refusal := _item_refusal(item_id):
        return refusal
    if site_answer is not None and decision != "use_saved_login":
        return "site_answer goes only with use_saved_login."
    if auto_accept and decision != "accept":
        return "auto_accept goes only with accept."
    if credential_ids is not None and decision not in ("approve", "use_saved_login"):
        return "credential_ids goes only with approve or use_saved_login."
    if decision == "use_saved_login" and (credential_ids is None or len(credential_ids) != 1):
        return "use_saved_login needs exactly one credential id in credential_ids."
    return None


def _approval_ids(item: dict[str, Any], kind: str, credential_ids: list[str] | None) -> list[str] | str:
    """The credential ids to send with an approval, or the refusal text."""
    if kind == "run_group_cancel":
        return "Cancelling a group takes no credential_ids." if credential_ids is not None else []
    review = item.get("account_group_review") or item.get("credential_delete_review") or {}
    row_ids = [str(row["credential_id"]) for row in review.get("rows") or [] if isinstance(row, dict)]
    if kind == "run_approval" and review.get("single_run"):
        if credential_ids is not None:
            return "A single run covers every account; leave credential_ids out."
        return row_ids
    if not credential_ids:
        return "Approving needs credential_ids: the ones the user chose from the item's facts."
    unknown = sorted(set(credential_ids) - set(row_ids))
    if unknown:
        return f"These credential ids are not on the item: {unknown}. Choose from {row_ids}."
    return credential_ids


def _record_outcome(record: dict[str, Any] | None) -> dict[str, Any]:
    record = record or {}
    outcome: dict[str, Any] = {"status": record.get("status")}
    deletion = record.get("credential_delete_review") or {}
    if deletion.get("outcomes") is not None:
        outcome["deletions"] = [
            {"credential_id": row.get("credential_id"), "outcome": row.get("outcome")}
            for row in deletion["outcomes"]
            if isinstance(row, dict)
        ]
    review = record.get("account_group_review") or {}
    if review.get("workflow_run_id"):
        outcome["workflow_run_id"] = review["workflow_run_id"]
    return outcome


def _account_group_decision(item: dict[str, Any], decision: Decision, ids: list[str]) -> dict[str, Any]:
    review = item.get("account_group_review") or {}
    # An input-group card keys each run by its row, and the server reads those keys only from item_keys.
    if decision == "approve" and review.get("row_group") and not review.get("single_run"):
        return {"approved": True, "item_keys": ids}
    return {"approved": decision == "approve", "credential_ids": ids}


def _stored_choice(record: dict[str, Any], kind: str) -> tuple[bool, list[str]] | None:
    """The decision the server holds for an approval or deletion item, or None when the record does not say."""
    if kind == "credential_deletion":
        response = record.get("response")
        if not isinstance(response, dict):
            return None
        approved = not response.get("skipped", False)
        rows = (record.get("credential_delete_review") or {}).get("outcomes") or []
        return approved, [str(row.get("credential_id")) for row in rows if isinstance(row, dict)] if approved else []
    for key in ("account_group_review", "account_group_cancel"):
        stored = (record.get(key) or {}).get("decision")
        if isinstance(stored, dict):
            approved = bool(stored.get("approved"))
            item_keys = stored.get("item_keys")
            chosen = item_keys if item_keys is not None else stored.get("credential_ids") or []
            return approved, [str(cid) for cid in chosen] if approved else []
    # A skipped review or cancel card stores no decision, and nothing runs or stops.
    response = record.get("response")
    if isinstance(response, dict) and response.get("skipped"):
        return False, []
    return None


async def _decide_question(
    action: str,
    chat_id: str,
    item: dict[str, Any],
    decision: Decision,
    credential_ids: list[str] | None,
    deadline: float,
    timer: Timer,
    *,
    allowed: list[str],
) -> dict[str, Any]:
    item_id = str(item.get("interaction_id"))
    kind = _question_action(item, None, None)["kind"]
    if kind == "question":
        return _error(
            action,
            ErrorCode.INVALID_INPUT,
            f"{item_id} is a question; pass the user's answer to skyvern_ember_answer.",
            timer,
        )
    if decision not in allowed:
        return _not_allowed(action, item_id, decision, allowed, timer)
    sent_ids: list[str] = []
    if decision == "decline" and kind == "credential_deletion":
        record = await post_action(
            "question-response",
            {"ember_chat_id": chat_id, "interaction_id": item_id, "skipped": True},
            timeout=READ_TIMEOUT_SECONDS,
        )
    else:
        ids = _approval_ids(item, kind, credential_ids) if decision == "approve" else []
        if isinstance(ids, str):
            return _error(action, ErrorCode.INVALID_INPUT, ids, timer)
        sent_ids = ids
        if kind == "credential_deletion":
            record = await post_action(
                "credential-deletion",
                {"ember_chat_id": chat_id, "interaction_id": item_id, "credential_ids": ids},
                timeout=READ_TIMEOUT_SECONDS,
            )
        else:
            record = await post_action(
                "question-response",
                {
                    "ember_chat_id": chat_id,
                    "interaction_id": item_id,
                    "account_group_decision": _account_group_decision(item, decision, ids),
                },
                timeout=READ_TIMEOUT_SECONDS,
            )
    outcome = _record_outcome(record)
    # An item answered elsewhere first comes back as its stored record with 200, so compare before reporting.
    stored = _stored_choice(record or {}, kind)
    if stored is not None:
        outcome |= {"approved": stored[0], "credential_ids": stored[1]}
        approved = decision == "approve"
        if stored[0] != approved or (
            approved and kind != "credential_deletion" and sorted(stored[1]) != sorted(sent_ids)
        ):
            return _error(
                action,
                ErrorCode.ITEM_EXPIRED,
                f"{item_id} was already {'approved' if stored[0] else 'declined'} before this call, so this decision "
                "changed nothing. Tell the user what was decided.",
                timer,
                data={"decision": "approve" if stored[0] else "decline", "outcome": outcome},
            )
    extra = {"decision": decision, "outcome": outcome}
    return await _result_after(action, chat_id, deadline, timer, extra)


async def _decide_login(
    action: str,
    chat_id: str,
    history: dict[str, Any],
    item_id: str,
    decision: Decision,
    credential_id: str | None,
    site_answer: str | None,
    deadline: float,
    timer: Timer,
) -> dict[str, Any]:
    card = _login_card(history, item_id)
    if card is None:
        expired_next = _expired_pick_next(credential_id) if credential_id is not None else None
        return await _expired(action, item_id, history, timer, deadline=deadline, next_text=expired_next)
    allowed = _login_decisions(card)
    if decision not in allowed:
        return _not_allowed(action, item_id, decision, allowed, timer)
    if site_answer is not None:
        question = card.get("site_question") or {}
        if question.get("credential_id") != credential_id:
            return _error(
                action,
                ErrorCode.INVALID_INPUT,
                "site_answer answers the site question this card asked about that login. Pick the login without "
                "site_answer first.",
                timer,
            )
        if site_answer == "add" and question.get("vault_sourced"):
            return _error(
                action,
                ErrorCode.INVALID_INPUT,
                "This login's sites come from a connected vault, so only the vault can add a site. Pass the user's "
                "answer as site_answer='no', or have them add the site in their vault.",
                timer,
            )
    pause = {"turn_id": card.get("turn_id"), "ember_chat_id": chat_id, "resume_token": card.get("resume_token")}
    next_text = None
    if decision == "generate_login":
        # Saving a generated login can outlast a read; give it the caller's whole remaining wait.
        outcome = (
            await post_action("credential-generate", pause, timeout=max(READ_TIMEOUT_SECONDS, _remaining(deadline)))
            or {}
        )
        if outcome.get("result") in ("created_not_connected", "unknown"):
            next_text = (
                "The login may already be saved; links.credentials shows whether it is, and "
                "decision='use_saved_login' passes a saved one."
            )
        return await _result_after(
            action,
            chat_id,
            deadline,
            timer,
            {"decision": decision, "outcome": outcome},
            next_text,
            login_resolved=True,
        )
    body = pause | {"action": _LOGIN_ACTIONS[decision]}
    if decision == "use_saved_login":
        body |= {"credential_id": credential_id, "asks_site": True}
        if site_answer is not None:
            body["site_answer"] = site_answer
    outcome = await post_action("credential-response", body, timeout=READ_TIMEOUT_SECONDS) or {}
    if outcome.get("result") == "site_changed_not_connected" and credential_id is not None:
        next_text = _expired_pick_next(credential_id)
    elif outcome.get("result") == "site_change_unknown":
        next_text = (
            "The site save was not confirmed, and Ember's paused turn reads this login back itself; "
            f"skyvern_ember_check with ember_chat_id={chat_id!r} shows whether Ember went on."
        )
    elif outcome.get("result") == "site_question" and isinstance(outcome.get("site_question"), dict):
        facts = _site_question_facts(outcome["site_question"])
        outcome["site_question"] = facts
        answers = "site_answer='no'" if facts["vault_sourced"] else "site_answer='add' if they allow it, or 'no'"
        next_text = (
            f"Ask the user before going on: {facts['consent']} Then call skyvern_ember_decide with "
            f"decision='use_saved_login', the same credential and {answers}."
        )
    if decision == "sign_in_myself":
        session = await get_browser_session(
            str(card.get("sign_in_browser_session_id")), timeout=_poll_timeout(deadline)
        )
        outcome["live_browser_url"] = (session or {}).get("app_url")
        where = "outcome.live_browser_url" if outcome["live_browser_url"] else "the Studio at links.studio"
        next_text = (
            f"Have the user sign in themselves in {where} before outcome.expires_at. Once they say they are done, "
            f"call skyvern_ember_decide with decision='signed_in' and item_id={item_id!r}."
        )
    return await _result_after(
        action,
        chat_id,
        deadline,
        timer,
        {"decision": decision, "outcome": outcome},
        next_text,
        login_resolved=True,
    )


async def _decide_proposal(
    action: str,
    chat_id: str,
    history: dict[str, Any],
    item_id: str,
    decision: Decision,
    auto_accept: bool,
    deadline: float,
    timer: Timer,
) -> dict[str, Any]:
    rows = _rows(history)
    proposal = _proposal(history, rows)
    if proposal is None:
        return _error(action, ErrorCode.NO_PROPOSAL, "This chat has no proposal waiting.", timer)
    if proposal["proposal_id"] is None:
        return _error(
            action,
            ErrorCode.INVALID_INPUT,
            "This proposal has no proposal_id, so it can only be accepted or rejected in the Studio at links.studio.",
            timer,
        )
    if proposal["proposal_id"] != item_id:
        return _error(
            action,
            ErrorCode.PROPOSAL_CHANGED,
            f"The proposal is now {proposal['proposal_id']}, not {item_id}. Nothing was saved.",
            timer,
            data=await _turn_state_with_links(history, since=None, deadline=deadline),
        )
    shown = map_turn_state(history, now=datetime.now(UTC))["proposal"]
    if shown is None or shown["proposal_id"] != item_id:
        return _error(
            action,
            ErrorCode.INVALID_INPUT,
            f"{item_id}'s review was too large to show in full here, so the user accepts or rejects it in the "
            f"Studio at {_links(history)['studio']}. Nothing was saved.",
            timer,
        )
    metadata = history.get("proposed_workflow_metadata") or {}
    current_auto_accept = bool(history.get("auto_accept"))
    body = {
        "ember_chat_id": chat_id,
        "owner_turn_id": metadata.get("owner_turn_id"),
        "revision": metadata.get("revision"),
        "auto_accept": current_auto_accept or (decision == "accept" and auto_accept),
    }
    if decision == "accept":
        workflow = await post_action("apply-proposed-workflow", body, timeout=READ_TIMEOUT_SECONDS) or {}
        outcome = {"accepted": True, "version": workflow.get("version"), "auto_accept": body["auto_accept"]}
    else:
        await post_action("clear-proposed-workflow", body, timeout=READ_TIMEOUT_SECONDS)
        outcome = {"rejected": True, "auto_accept": body["auto_accept"]}
    return await _result_after(action, chat_id, deadline, timer, {"decision": decision, "outcome": outcome})


async def skyvern_ember_decide(
    ember_chat_id: Annotated[str, Field(description="The chat whose item to decide.")],
    decision: Annotated[Decision, Field(description="The user's choice; one of the item's allowed_decisions.")],
    item_id: Annotated[
        str | None,
        Field(
            description=(
                "user_actions[].item_id, or proposal.proposal_id for accept and reject. Leave out for "
                "turn_off_auto_accept."
            )
        ),
    ] = None,
    credential_ids: Annotated[
        list[str] | None,
        Field(
            description=(
                "approve: the ones the user chose. use_saved_login: exactly one saved credential id, such as one in "
                "the card's saved_logins or its named_credential_id; a login the user wants that isn't listed can also "
                "be picked in the Studio at the card's link."
            )
        ),
    ] = None,
    site_answer: Annotated[
        Literal["add", "no"] | None,
        Field(description="use_saved_login only: the user's answer to the item's site_question."),
    ] = None,
    auto_accept: Annotated[
        bool, Field(description="accept only: true only when the user said to always accept Ember's drafts.")
    ] = False,
    wait_seconds: Annotated[
        int,
        Field(
            description=(
                "Deadline for the whole call, in seconds, including the wait for Ember to go on. Below 20, the "
                "call still takes up to 20 seconds to read the chat and send the decision."
            ),
            ge=0,
            le=MAX_WAIT_SECONDS,
        ),
    ] = DEFAULT_DECISION_WAIT_SECONDS,
) -> dict[str, Any]:
    """Pass the user's decision on an item Ember (the new name for Skyvern's Workflow Copilot; the app may still
    say Copilot) is waiting on: approve or decline runs and credential deletions, pick a saved login for a login
    card, generate one, sign in themselves, accept or reject a proposal, or turn off auto-accept. Show the item to
    the user first and call this only with the choice they made; never choose for them. Each call reads the chat
    again, so a draft that changed since the user reviewed it is refused (PROPOSAL_CHANGED). It returns the turn
    state, like skyvern_ember_check, with the server's outcome.
    """
    action_name = "skyvern_ember_decide"
    with Timer() as timer:
        deadline = time.monotonic() + wait_seconds
        if refusal := _decide_refusal(decision, item_id, credential_ids, site_answer, auto_accept):
            return _error(action_name, ErrorCode.INVALID_INPUT, refusal, timer)
        credential_id = credential_ids[0] if decision == "use_saved_login" and credential_ids else None
        extra: dict[str, Any] = {"decision": decision}
        try:
            if decision == "turn_off_auto_accept":
                await post_action("disable-auto-accept", {"ember_chat_id": ember_chat_id}, timeout=READ_TIMEOUT_SECONDS)
                extra["outcome"] = {"auto_accept": False}
                return await _result_after(action_name, ember_chat_id, deadline, timer, extra)
            if item_id is None:
                return _error(action_name, ErrorCode.INVALID_INPUT, "item_id is required for this decision.", timer)
            history = await get_history(chat_id=ember_chat_id, timeout=READ_TIMEOUT_SECONDS)
            if refusal := _too_large_refusal(history, item_id):
                return _error(action_name, ErrorCode.INVALID_INPUT, refusal, timer)
            if item_id.startswith(LOGIN_ITEM_PREFIX):
                return await _decide_login(
                    action_name, ember_chat_id, history, item_id, decision, credential_id, site_answer, deadline, timer
                )
            if question := _pending_question(history, item_id):
                offered = {
                    action.get("item_id"): action.get("allowed_decisions")
                    for action in map_turn_state(history, now=datetime.now(UTC), fit=False)["user_actions"]
                }
                return await _decide_question(
                    action_name,
                    ember_chat_id,
                    question,
                    decision,
                    credential_ids,
                    deadline,
                    timer,
                    allowed=offered.get(item_id) or ["approve", "decline"],
                )
            if decision not in _PROPOSAL_DECISIONS:
                rows = _rows(history)
                proposal = _proposal(history, rows)
                if proposal is not None and proposal["proposal_id"] == item_id:
                    return _not_allowed(action_name, item_id, decision, list(_PROPOSAL_DECISIONS), timer)
                return await _expired(action_name, item_id, history, timer, deadline=deadline)
            if not _is_proposal_id(item_id):
                return await _expired(action_name, item_id, history, timer, deadline=deadline)
            return await _decide_proposal(
                action_name, ember_chat_id, history, item_id, decision, auto_accept, deadline, timer
            )
        except SitesChanged as exc:
            extra["outcome"] = {"result": "sites_changed", "detail": exc.message}
            sites_next = f"{exc.message} Show the user the login item again, and pass their new choice."
            return await _result_after(action_name, ember_chat_id, deadline, timer, extra, sites_next)
        except EmberError as exc:
            if exc.code == ErrorCode.ITEM_EXPIRED and credential_id is not None:
                return _error(
                    action_name, exc.code, exc.message, timer, data={"next": _expired_pick_next(credential_id)}
                )
            return _ember_error(action_name, exc, timer)
