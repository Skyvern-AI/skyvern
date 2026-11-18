from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import AliasChoices

from skyvern.cli.mcp_tools import _ember_http, _workflow_http, ember, mcp
from skyvern.client.core.client_wrapper import AsyncClientWrapper
from skyvern.forge.sdk.copilot.context import NarrativeCreatedTrigger
from skyvern.forge.sdk.copilot.interruption import INTERRUPTED_TERMINAL_REASON
from skyvern.forge.sdk.copilot.turn_outcome import CANCEL_TERMINAL_REASON
from skyvern.forge.sdk.routes import workflow_copilot
from skyvern.forge.sdk.schemas.workflow_copilot import (
    CopilotPendingTurn,
    CopilotTurnControl,
    WorkflowCopilotChatHistoryResponse,
    WorkflowCopilotChatRequest,
)
from tests.unit.scoped_asyncio import ScopedAsyncio

API_KEY = "sk_test_ember"
WPID = "wpid_ember_1"
CHAT = "wcc_ember_1"
OTHER_CHAT = "wcc_ember_2"
TURN = "turn-ember-1"
CANCEL_TOKEN = "cancel-token-value-7f3a"
RESUME_TOKEN = "resume-token-value-91bc"
APP_URL = "https://app.example.test"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
STARTED = (NOW - timedelta(seconds=90)).isoformat()

EXPECTED_CHAT_POST_FLAGS = {
    "workflow_yaml": "",
    "mode": "build",
    "doc_editor_enabled": True,
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


def _activity(count: int, *, text: str = "Opened the page") -> list[dict[str, Any]]:
    entries = [
        {
            "id": f"a{index}",
            "kind": "tool",
            "text": f"{text} {index}",
            "iteration": index,
            "toolName": "navigate_browser",
            "success": True,
            "timestamp": STARTED,
        }
        for index in range(count)
    ]
    entries.append(
        {"id": "hidden", "kind": "tool", "text": "screenshot", "iteration": 99, "toolName": "get_run_results"}
    )
    return entries


def _narrative(*, terminal: str = "running", **extra: Any) -> dict[str, Any]:
    return {
        "turnId": TURN,
        "turnIndex": 0,
        "designStarted": True,
        "designEnded": terminal != "running",
        "draft": {"blockCount": 1, "blockLabels": ["open_page"], "summary": None},
        "blocks": [
            {
                "label": "open_page",
                "blockType": "navigation",
                "state": "running",
                "lastSeenIteration": 1,
                "activity": [],
                "startedAt": STARTED,
                "endedAt": None,
                "workflowRunId": "wr_1",
            }
        ],
        "terminal": terminal,
        "terminalMessage": None,
        "narrativeSummary": None,
        "priorBlockCount": 0,
        "designActivity": _activity(3),
        "startedAt": STARTED,
        "endedAt": None if terminal == "running" else NOW.isoformat(),
        **extra,
    }


def _message(sender: str, content: str, **extra: Any) -> dict[str, Any]:
    return {"sender": sender, "content": content, "created_at": NOW.isoformat(), "turn_id": TURN, **extra}


def _history(
    *,
    in_flight: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    chat_id: str | None = CHAT,
    **extra: Any,
) -> dict[str, Any]:
    raw = {
        "workflow_copilot_chat_id": chat_id,
        "workflow_permanent_id": WPID if chat_id else None,
        "app_url": APP_URL,
        "in_flight_turn": in_flight,
        "chat_history": messages or [],
        **extra,
    }
    return WorkflowCopilotChatHistoryResponse.model_validate(raw).model_dump(mode="json")


def _live(**narrative_extra: Any) -> dict[str, Any]:
    narrative = _narrative(browserSessionId=None, pausedFor=None)
    narrative.update(narrative_extra)
    return {"turn_id": TURN, "cancel_token": CANCEL_TOKEN, "narrative": narrative}


def working_history() -> dict[str, Any]:
    return _history(in_flight=_live(), messages=[_message("user", "Build it")])


def _question(**extra: Any) -> dict[str, Any]:
    return {
        "interaction_id": "q_1",
        "turn_id": TURN,
        "tool_call_id": "call_1",
        "parts": [{"part_id": "p1", "prompt": "Which site?", "choices": [{"choice_id": "c1", "text": "Site A"}]}],
        **extra,
    }


def _login_card() -> dict[str, Any]:
    return {
        "turn_id": TURN,
        "workflow_copilot_chat_id": CHAT,
        "resume_token": RESUME_TOKEN,
        "reason": "login_credentials_unresolved",
        "message": "I need a login for the site.",
        "timeout_seconds": 600,
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "timestamp": NOW.isoformat(),
    }


def _finished_row(**payload_extra: Any) -> dict[str, Any]:
    return _message(
        "ai",
        "I built the workflow.",
        narrative_payload=_narrative(terminal="response", **payload_extra),
        turn_outcome={"response_kind": "build", "copilot_turn_id": TURN},
    )


RECORDED_HISTORY_DIR = Path(__file__).parent / "fixtures" / "ember_chat_history"
# Each file is one GET /v1/ember/chat-history response recorded from a local Ember turn; PROVENANCE.md names its run.
RECORDED_MAPPER_CASES: dict[str, tuple[str, str | None, list[str], str]] = {
    "working": ("working", None, [], "skyvern_ember_check"),
    "question": ("waiting_for_user", "you", ["question"], "skyvern_ember_answer"),
    "question_just_resolved": ("working", None, [], "skyvern_ember_check"),
    "run_approval": ("waiting_for_user", "you", ["run_approval"], "skyvern_ember_decide"),
    "login_card": ("waiting_for_user", "you", ["login_needed"], "skyvern_ember_decide"),
    "registration_card": ("waiting_for_user", "you", ["login_needed"], "skyvern_ember_decide"),
    "site_question": ("waiting_for_user", "you", ["login_needed"], "skyvern_ember_decide"),
    "waiting_on_another_person": (
        "waiting_for_user",
        "the person who started this turn",
        [],
        "person who started this turn",
    ),
    "finished_tested_proposal": ("finished", None, [], "item_id=proposal.proposal_id"),
    "finished_untested_proposal": ("finished", None, [], "item_id=proposal.proposal_id"),
    "created_trigger_webhook": ("finished", None, ["trigger_created"], "item_id=proposal.proposal_id"),
    "stopped": ("stopped", None, [], "skyvern_ember_send"),
    "interrupted": ("interrupted", None, [], "skyvern_ember_send"),
}


def _recorded_history(case: str) -> dict[str, Any]:
    return json.loads((RECORDED_HISTORY_DIR / f"{case}.json").read_text())


def _recorded_token_values(history: dict[str, Any]) -> set[str]:
    in_flight = history.get("in_flight_turn") or {}
    cards = history.get("pending_credential_requests") or []
    values = {in_flight.get("cancel_token"), history.get("pending_question_cancel_token")}
    values |= {card.get("resume_token") for card in cards}
    return {value for value in values if value}


def _admin_sees_a_hidden_question() -> dict[str, Any]:
    return _recorded_history("question_just_resolved") | {"pending_question_cancel_token": "hidden-question-token"}


def _claimed_deletion_on_a_controlled_turn() -> dict[str, Any]:
    claimed = _question(
        credential_delete_review={
            "rows": [{"credential_id": "cred_1", "name": "Main", "credential_type": "password"}],
            "total_credential_count": 1,
            "claimed_at": NOW.isoformat(),
            "approved_credential_ids": ["cred_1"],
        }
    )
    return _history(in_flight=_live(pausedFor="question"), question_interactions=[claimed]) | {
        "pending_question_cancel_token": "question-token"
    }


def _ended_with_server_reason(case: str, terminal_reason: str) -> dict[str, Any]:
    history = _recorded_history(case)
    last_ai = next(row for row in reversed(history["chat_history"]) if row["sender"] == "ai")
    last_ai["turn_outcome"]["terminal_reason"] = terminal_reason
    last_ai["narrative_payload"]["cancelled"] = False
    return history


def _run_approval_with_repeat_risk() -> dict[str, Any]:
    history = _recorded_history("run_approval")
    rows = [{"credential_id": f"cred_{index}", "label": f"Account {index}"} for index in range(25)]
    rows[3] |= {"preselected": False, "prior_outcome": "completed"}
    rows[24] |= {"preselected": False, "prior_outcome": "unknown", "inputs": {"amount": 120}}
    rows[0] |= {"parameter_key": "source_login"}
    rows[1] |= {"parameter_key": "destination_login"}
    history["question_interactions"][0]["account_group_review"] |= {
        "single_run": False,
        "rows": rows,
        "common_inputs": {"invoice_month": "2026-10"},
        "credential_parameter_key": "account_login",
        "credential_parameter_used_by": [],
    }
    return history


def _question_with_a_detail_choice() -> dict[str, Any]:
    history = _recorded_history("question")
    pending = next(item for item in history["question_interactions"] if item["status"] == "pending")
    pending["parts"][0]["choices"][0]["detail_prompt"] = "Which account number?"
    return history


def _full_page_run_approval() -> dict[str, Any]:
    # A full page of an input-row group, the largest card the server sends: 64-character row keys and three inputs.
    history = _recorded_history("run_approval")
    rows = []
    for index in range(25):
        inputs = {"invoice_month": "2026-10", "vendor": f"Example Supplies {index:02d}", "amount": 1000 + index}
        rows.append(
            {
                "credential_id": hashlib.sha256(f"row-{index}".encode()).hexdigest(),
                "label": ", ".join(f"{key}: {value}" for key, value in inputs.items()),
                "inputs": inputs,
            }
        )
    history["question_interactions"][0]["account_group_review"] |= {
        "single_run": False,
        "row_group": True,
        "max_concurrency": 3,
        "rows": rows,
        "common_inputs": {"report_format": "csv"},
    }
    return history


def _group_cancel_card(unfinished_count: int | None) -> dict[str, Any]:
    rows = [
        {
            "credential_id": f"cred_{index}",
            "label": f"Account {index}",
            "workflow_run_id": f"wr_{index}",
            "run_status": "running",
            "outcome": "in_progress",
        }
        for index in range(25)
    ]
    review: dict[str, Any] = {"workflow_run_group_id": "wrg_1", "unfinished_rows": rows}
    if unfinished_count is not None:
        review["unfinished_count"] = unfinished_count
    return _history(
        in_flight=_live(pausedFor="question"),
        question_interactions=[_question(interaction_id="q_cancel", account_group_cancel=review)],
    )


def _deletion_with_thirty_credentials() -> dict[str, Any]:
    history = _deletion_history()
    review = history["question_interactions"][0]["credential_delete_review"]
    review["rows"] = [
        {"credential_id": f"cred_{index}", "name": f"Login {index}", "credential_type": "password"}
        for index in range(30)
    ]
    review["total_credential_count"] = 120
    return history


def _question_with_six_parts() -> dict[str, Any]:
    history = _recorded_history("question")
    pending = next(item for item in history["question_interactions"] if item["status"] == "pending")
    pending["parts"] = [pending["parts"][0] | {"part_id": f"part_{index}"} for index in range(6)]
    return history


def _cardless_credential_pause(*, caller_has_control: bool = True) -> dict[str, Any]:
    history = _recorded_history("login_card") | {"pending_credential_requests": []}
    if not caller_has_control:
        history["in_flight_turn"]["cancel_token"] = None
    return history


def _owner_review(history: dict[str, Any]) -> dict[str, Any] | None:
    owner_turn_id = (history.get("proposed_workflow_metadata") or {}).get("owner_turn_id")
    owner = next(
        (row for row in history["chat_history"] if row["sender"] == "ai" and row.get("turn_id") == owner_turn_id),
        None,
    )
    return ((owner or {}).get("narrative_payload") or {}).get("review")


def _proposal_with_review_blocks(count: int | None) -> dict[str, Any]:
    history = _recorded_history("finished_tested_proposal") | {"workflow_copilot_chat_id": CHAT}
    owner_turn_id = history["proposed_workflow_metadata"]["owner_turn_id"]
    owner = next(
        row for row in history["chat_history"] if row["sender"] == "ai" and row.get("turn_id") == owner_turn_id
    )
    if count is None:
        del owner["narrative_payload"]["review"]
    else:
        owner["narrative_payload"]["review"]["blocks"] = [
            {"label": f"extract_section_{index}", "blockType": "code", "change": "modified", "neverTested": False}
            for index in range(count)
        ]
        owner["narrative_payload"]["review"]["duplicateWrites"] = [
            {"blockType": "file_upload", "blockLabels": ["extract_section_0", "extract_section_1"]}
        ]
    return history


def _auto_applicable_proposal() -> dict[str, Any]:
    history = _recorded_history("finished_tested_proposal")
    history["proposed_workflow_metadata"]["disposition"] = "auto_applicable"
    return history


def _proposal_owner_outside_the_window() -> dict[str, Any]:
    history = _recorded_history("finished_untested_proposal")
    metadata = history["proposed_workflow_metadata"]
    for row in history["chat_history"]:
        if row.get("turn_id") == metadata["owner_turn_id"]:
            row["turn_id"] = "turn-after-the-proposal"
    metadata["disposition"] = "accepting"
    return history


STARTER = ("waiting_for_user", "the person who started this turn", [], "person who started this turn")
# Recorded payloads edited to reach a branch no recording covers, or to carry the server's own terminal reasons.
EDITED_MAPPER_CASES: dict[str, tuple[Callable[[], dict[str, Any]], tuple[str, str | None, list[str], str]]] = {
    "admin_sees_a_hidden_question": (_admin_sees_a_hidden_question, STARTER),
    "claimed_deletion_on_a_controlled_turn": (
        _claimed_deletion_on_a_controlled_turn,
        ("working", None, [], "skyvern_ember_check"),
    ),
    "server_cancel_reason": (
        lambda: _ended_with_server_reason("stopped", CANCEL_TERMINAL_REASON),
        ("stopped", None, [], "skyvern_ember_send"),
    ),
    "server_user_cancelled_reason": (
        lambda: _ended_with_server_reason("stopped", workflow_copilot.USER_CANCELLED_TERMINAL_REASON),
        ("stopped", None, [], "skyvern_ember_send"),
    ),
    "server_interrupted_reason": (
        lambda: _ended_with_server_reason("interrupted", INTERRUPTED_TERMINAL_REASON),
        ("interrupted", None, [], "skyvern_ember_send"),
    ),
    "run_approval_with_repeat_risk": (
        _run_approval_with_repeat_risk,
        ("waiting_for_user", "you", ["run_approval"], "skyvern_ember_decide"),
    ),
    "question_with_a_detail_choice": (
        _question_with_a_detail_choice,
        ("waiting_for_user", "you", ["question"], "skyvern_ember_answer"),
    ),
    "full_page_run_approval": (
        _full_page_run_approval,
        ("waiting_for_user", "you", ["run_approval"], "skyvern_ember_decide"),
    ),
    "cancel_with_a_hundred_unfinished_runs": (
        partial(_group_cancel_card, 100),
        ("waiting_for_user", "you", ["run_group_cancel"], "skyvern_ember_decide"),
    ),
    "cancel_without_a_server_count": (
        partial(_group_cancel_card, None),
        ("waiting_for_user", "you", ["run_group_cancel"], "skyvern_ember_decide"),
    ),
    "deletion_with_thirty_credentials": (
        lambda: _deletion_with_thirty_credentials(),
        ("waiting_for_user", "you", ["credential_deletion"], "skyvern_ember_decide"),
    ),
    "question_with_six_parts": (
        _question_with_six_parts,
        ("waiting_for_user", "you", ["question"], "skyvern_ember_answer"),
    ),
    "proposal_owner_outside_the_window": (
        _proposal_owner_outside_the_window,
        ("finished", None, [], "skyvern_ember_send"),
    ),
    "created_webhook_and_gmail_triggers": (
        lambda: _created_triggers_history(),
        ("finished", None, ["trigger_created", "trigger_created"], "item_id=proposal.proposal_id"),
    ),
    "cardless_credential_pause": (_cardless_credential_pause, STARTER),
    "cardless_credential_pause_after_this_callers_login_decision": (
        _cardless_credential_pause,
        ("working", None, [], "skyvern_ember_check"),
    ),
    "cardless_credential_pause_after_another_members_login_decision": (
        partial(_cardless_credential_pause, caller_has_control=False),
        STARTER,
    ),
    # Fully tested with auto-accept off, the default: the draft is still unsaved until someone accepts it.
    "auto_applicable_proposal": (_auto_applicable_proposal, ("finished", None, [], "item_id=proposal.proposal_id")),
}
# Read as the call right after this caller's own login decision.
LOGIN_DECIDED_CASES = frozenset(
    {
        "cardless_credential_pause_after_this_callers_login_decision",
        "cardless_credential_pause_after_another_members_login_decision",
    }
)
MAPPER_CASES = {
    case: (partial(_recorded_history, case), expected) for case, expected in RECORDED_MAPPER_CASES.items()
} | EDITED_MAPPER_CASES


@pytest.mark.parametrize("case", sorted(MAPPER_CASES))
def test_mapper_contract_on_recorded_history_payloads(case: str) -> None:
    build, (state, waiting_on, kinds, next_hint) = MAPPER_CASES[case]
    history = build()
    WorkflowCopilotChatHistoryResponse.model_validate(history)

    result = ember.map_turn_state(history, now=NOW, login_resolved=case in LOGIN_DECIDED_CASES)

    assert result["state"] == state
    assert result["waiting_on"] == waiting_on
    assert [action["kind"] for action in result["user_actions"]] == kinds
    assert next_hint in result["next"]
    assert len(json.dumps(result)) <= ember.SUMMARY_LIMIT_CHARS
    output = json.dumps(result)
    assert not [token for token in _recorded_token_values(history) if token in output]
    wpid, chat = history["workflow_permanent_id"], history["workflow_copilot_chat_id"]
    assert result["links"]["studio"] == f"{history['app_url']}/agents/{wpid}/studio?chat={chat}"
    for action in result["user_actions"]:
        if action["kind"] == "trigger_created":
            assert (action["allowed_decisions"], action["link"]) == ([], result["links"]["studio"]) and action["reason"]
    if metadata := history.get("proposed_workflow_metadata"):
        review = _owner_review(history)
        # Accepting saves the whole draft, so there is an id to accept by only beside the draft's review.
        expected_id = f"{metadata['owner_turn_id']}:{metadata['revision']}" if review is not None else None
        assert result["proposal"]["proposal_id"] == expected_id
        assert result["proposal"]["tested"] is {
            "review_tested": True,
            "review_untested": False,
            "auto_applicable": True,
        }.get(metadata["disposition"])
        assert (result["proposal"]["changes"] is None) is (review is None)
        # Whether the review behind the changes was shown is a field, not something a caller infers from nulls.
        assert result["proposal"]["review"] == ("missing" if review is None else "shown")
    items = {item["interaction_id"]: item for item in history.get("question_interactions") or []}
    for action in result["user_actions"]:
        if action["kind"] == "question":
            parts = items[action["item_id"]]["parts"]
            assert [option["part_id"] for option in action["options"]] == [part["part_id"] for part in parts]
            detail_keys = [
                [{key: choice[key] for key in choice if key == "detail_prompt"} for choice in option["choices"]]
                for option in action["options"]
            ]
            assert detail_keys == [
                [
                    {"detail_prompt": choice["detail_prompt"]} if choice.get("detail_prompt") else {}
                    for choice in part["choices"][:10]
                ]
                for part in parts
            ]
        if action["kind"] == "run_approval":
            review = items[action["item_id"]]["account_group_review"]
            facts = action["facts"]
            assert facts["total_accounts"] == len(review["rows"]) and facts["preselected_note"]
            shown = (
                facts["workflow_title"],
                facts["version"],
                facts["row_group"],
                facts["max_concurrency"],
                facts["tested_workflow_run_id"],
            )
            assert shown == (
                review["workflow_title"],
                review["version"],
                review.get("row_group", False),
                review.get("max_concurrency"),
                review.get("tested_workflow_run_id"),
            )
            assert "based_on_inputs" not in json.dumps(facts)
            assert facts["common_inputs"] == (review.get("common_inputs") or {})
            if review.get("single_run") or review.get("row_group"):
                assert facts["warnings"] == []
            else:
                unused_login = review.get("credential_parameter_used_by") == []
                assert len(facts["warnings"]) == unused_login + (not review.get("clean_group_run_id"))
                named = [warning for warning in facts["warnings"] if review["credential_parameter_key"] in warning]
                assert bool(named) is unused_login
            # Unset fields are left out so a full page fits; every set one is shown as the server sent it.
            assert facts["accounts"] == [
                {
                    "credential_id": row["credential_id"],
                    **({} if row.get("inputs") else {"label": row["label"]}),
                    "preselected": row.get("preselected", True),
                    **{key: row[key] for key in ("prior_outcome", "inputs", "parameter_key") if row.get(key)},
                }
                for row in review["rows"]
            ]
            assert action["allowed_decisions"] == ["approve", "decline"] and action["prompt"]
        if action["kind"] == "run_group_cancel":
            review = items[action["item_id"]]["account_group_cancel"]
            unfinished = review.get("unfinished_count")
            assert action["facts"]["unfinished_runs"] == unfinished
            assert [(run["label"], run["workflow_run_id"]) for run in action["facts"]["listed_runs"]] == [
                (row["label"], row["workflow_run_id"]) for row in review["unfinished_rows"]
            ]
            assert (f"all {unfinished}" if unfinished is not None else "every unfinished run") in action["prompt"]
        if action["kind"] == "credential_deletion":
            review = items[action["item_id"]]["credential_delete_review"]
            assert [row["credential_id"] for row in action["facts"]["credentials"]] == [
                row["credential_id"] for row in review["rows"]
            ]
            assert action["facts"]["organization_credential_count"] == review["total_credential_count"]


def _oversized_items_history() -> dict[str, Any]:
    long_url = "https://login.example.test/" + "p" * 3000
    questions = [
        _question(
            interaction_id=f"q_{index}",
            parts=[
                {
                    "part_id": f"p{part}",
                    "prompt": "Which one? " * 200,
                    "choices": [{"choice_id": f"c{choice}", "text": "n" * 2000} for choice in range(10)],
                }
                for part in range(5)
            ],
        )
        for index in range(6)
    ]
    card = {**_login_card(), "message": "m" * 5000, "login_page_urls": [long_url] * 5}
    live = _live(workPlan={"toolCallId": "call_plan", "items": ["w" * 2000] * 20})
    live["narrative"]["designActivity"] = _activity(400, text="x" * 2000)
    return _history(
        in_flight=live,
        messages=[_message("user", "u" * 20000)],
        question_interactions=questions,
        pending_credential_requests=[card],
        app_url="https://app.example.test/" + "a" * 300,
    )


def _long_approval_history() -> dict[str, Any]:
    review = _recorded_history("run_approval")["question_interactions"][0]["account_group_review"]
    approval = _question(interaction_id="q_run", account_group_review=review | {"action_summary": "Run it. " * 1500})
    return _history(in_flight=_live(), question_interactions=[approval])


def _long_review_change_history() -> dict[str, Any]:
    history = _recorded_history("finished_tested_proposal")
    owner_turn_id = history["proposed_workflow_metadata"]["owner_turn_id"]
    owner = next(
        row for row in history["chat_history"] if row["sender"] == "ai" and row.get("turn_id") == owner_turn_id
    )
    owner["narrative_payload"]["review"]["blocks"][0]["change"] = "c" * 12000
    return history


def _many_questions_history(count: int) -> dict[str, Any]:
    return _history(
        in_flight=_live(),
        question_interactions=[_question(interaction_id=f"q_{index}") for index in range(count)],
        app_url="https://app.example.test/" + "a" * 300,
    )


def _forced_fit_proposal_history() -> dict[str, Any]:
    history = _recorded_history("finished_tested_proposal")
    history["chat_history"][-1]["content"] = "r" * 40000
    triggers = _created_triggers_history()["chat_history"][-1]["narrative_payload"]["createdTriggers"]
    history["chat_history"][-1]["narrative_payload"]["createdTriggers"] = triggers * 30
    login = _recorded_history("login_card")["pending_credential_requests"][0]
    history["pending_credential_requests"] = [login | {"message": "m" * 5000}]
    return history


def _site_question_under_size_pressure() -> dict[str, Any]:
    history = _recorded_history("site_question")
    history["pending_credential_requests"][0]["site_question"]["site"] = "https://" + "portal-" * 30 + "example.com"
    parts = [
        {"part_id": f"part_{index}", "prompt": "Which report should Ember pull for this account? " * 8, "choices": []}
        for index in range(17)
    ]
    history.setdefault("question_interactions", []).append(
        _question(interaction_id="q_many", parts=parts, status="pending")
    )
    return history


def _acted_on_values(action: dict[str, Any]) -> list[Any]:
    facts = action.get("facts") or {}
    return [
        facts.get("common_inputs"),
        [(account.get("label"), account.get("inputs")) for account in facts.get("accounts") or []],
        ((facts.get("registration") or {}).get("username"), (facts.get("registration") or {}).get("credential_name")),
        [credential.get("name") for credential in facts.get("credentials") or []],
        (action.get("prompt"), action.get("options")) if action.get("kind") == "question" else None,
    ]


def _run_approval_with_long_account_labels() -> dict[str, Any]:
    history = _recorded_history("run_approval")
    # Labels are the credential name and masked username, so only their ends tell these accounts apart.
    rows = [
        {
            "credential_id": f"cred_{index}",
            "label": "Regional payroll portal login shared by the finance team " * 4 + f"#{index}",
        }
        for index in range(25)
    ]
    history["question_interactions"][0]["account_group_review"] |= {"single_run": False, "rows": rows}
    return history


def _registration_card_under_size_pressure() -> dict[str, Any]:
    history = _recorded_history("registration_card")
    card = history["pending_credential_requests"][0]
    card["registration"] = (card.get("registration") or {}) | {
        "username": "finance-team-shared-login-for-the-regional-payroll-portal-" + "x" * 30 + "@example.com",
        "credential_name": "Regional payroll portal, finance team shared login " * 3 + "(primary)",
    }
    parts = [
        {"part_id": f"part_{index}", "prompt": "Which report should Ember pull for this account? " * 8, "choices": []}
        for index in range(20)
    ]
    history.setdefault("question_interactions", []).append(
        _question(interaction_id="q_many", parts=parts, status="pending")
    )
    return history


def _question_with_a_long_prompt() -> dict[str, Any]:
    part = {
        "part_id": "part_0",
        "prompt": "Which statement should Ember download? " * 28 + "Answer only after checking the totals.",
        "choices": [{"choice_id": f"c_{index}", "text": f"Statement {index}"} for index in range(12)],
    }
    return working_history() | {
        "question_interactions": [_question(interaction_id="q_one", parts=[part], status="pending")]
    }


def _question_with_long_choices() -> dict[str, Any]:
    parts = [
        {
            "part_id": f"part_{part}",
            "prompt": f"Which report should Ember pull for account {part}?",
            "choices": [
                {
                    "choice_id": f"c_{part}_{choice}",
                    "text": "The monthly statement with " + "line items " * 40 + str(choice),
                }
                for choice in range(5)
            ],
        }
        for part in range(5)
    ]
    return working_history() | {
        "question_interactions": [_question(interaction_id="q_long", parts=parts, status="pending")]
    }


@pytest.mark.parametrize(
    ("build", "all_fit", "prompts_kept"),
    [
        (_oversized_items_history, True, False),
        (partial(_many_questions_history, 40), True, False),
        (partial(_many_questions_history, 300), False, False),
        (_long_approval_history, True, True),
        (_forced_fit_proposal_history, False, False),
        (lambda: _single_run_with_a_long_input(), True, True),
        (lambda: _deletion_with_long_names(), True, True),
        (_site_question_under_size_pressure, True, True),
        (_question_with_long_choices, True, True),
        (_question_with_a_long_prompt, True, True),
        (_run_approval_with_long_account_labels, True, True),
        (_registration_card_under_size_pressure, True, True),
        (partial(_proposal_with_review_blocks, 300), True, True),
        (partial(_proposal_with_review_blocks, 45), True, True),
        (partial(_proposal_with_review_blocks, None), True, True),
        (_long_review_change_history, True, True),
    ],
    ids=[
        "oversized_items",
        "40_questions",
        "300_questions",
        "long_approval_prompt",
        "proposal_with_many_triggers",
        "approval_with_a_clipped_input",
        "deletion_with_clipped_names",
        "site_question_under_size_pressure",
        "question_with_clipped_choices",
        "question_with_a_long_prompt_and_12_choices",
        "run_approval_with_clipped_account_labels",
        "registration_card_under_size_pressure",
        "proposal_review_too_large_to_show",
        "proposal_review_past_the_old_40_cap",
        "proposal_without_a_review",
        "long_review_change",
    ],
)
def test_mapper_fits_an_oversized_history_and_keeps_every_item_id(
    monkeypatch: pytest.MonkeyPatch, build: Callable[[], dict[str, Any]], all_fit: bool, prompts_kept: bool
) -> None:
    history = build()
    with monkeypatch.context() as unbounded:
        unbounded.setattr(ember, "SUMMARY_LIMIT_CHARS", sys.maxsize)
        full = ember.map_turn_state(history, now=NOW)

    result = ember.map_turn_state(history, now=NOW)

    assert len(json.dumps(result)) <= ember.SUMMARY_LIMIT_CHARS
    kept, every = result["user_actions"], full["user_actions"]
    assert [action["item_id"] for action in kept] == [action["item_id"] for action in every[: len(kept)]]
    demoted = []
    for action, original in zip(kept, every):
        # A card cut down to its id, or whose run inputs or credential names were clipped, is offered only in the
        # Studio, since nothing here shows in full what approving it acts on.
        cut = "prompt" not in action and "prompt" in original
        clipped = "prompt" in action and _acted_on_values(action) != _acted_on_values(original)
        assert action.get("allowed_decisions") == ([] if cut or clipped else original.get("allowed_decisions"))
        # A card shortened to fit says so on the card, and one whose acted-on values were clipped says why.
        assert action.get("cut") or not (cut or clipped)
        assert (action.get("reason") == ember._TOO_LARGE_REASON) is clipped
        answerable = original.get("allowed_decisions") or original.get("kind") == "question"
        if action["kind"] == "question" and "prompt" in action and not clipped:
            # An answer resolves the whole question, so an answerable card shows every part and choice whole.
            sent = next(
                item["parts"]
                for item in history["question_interactions"]
                if item["interaction_id"] == action["item_id"]
            )
            shown = [
                (option["prompt"], [choice["text"] for choice in option["choices"]]) for option in action["options"]
            ]
            assert shown == [(part["prompt"], [choice["text"] for choice in part["choices"]]) for part in sent]
        # Adding a site saves exactly the site the card names on the terms its consent states, so a kept card
        # never shows either clipped.
        if "prompt" in action and "site_question" in (original.get("facts") or {}):
            for key in ("site", "consent"):
                assert action["facts"]["site_question"][key] == original["facts"]["site_question"][key]
        demoted.append(bool((cut or clipped) and answerable))
    shown, review = result["proposal"], _owner_review(history)
    whole_review = (
        shown is not None
        and review is not None
        and [(change["label"], change["change"]) for change in shown["changes"] or []]
        == [(block["label"], block["change"]) for block in review["blocks"]]
        and shown["input_changes"] == review["inputs"]
        # The Studio shows steps that write to the same place beside Accept, so a whole review includes them.
        and shown["duplicate_writes"]
        == [
            {"block_type": group["blockType"], "block_labels": group["blockLabels"]}
            for group in review["duplicateWrites"]
        ]
    )
    if shown is not None:
        assert shown["review"] == ("missing" if review is None else "shown" if whole_review else "cut")
    # Accepting saves every change in the draft, so it is offered only beside the draft's whole review.
    assert (shown or {}).get("proposal_id") == ((full["proposal"] or {}).get("proposal_id") if whole_review else None)
    proposal_cut = bool((full["proposal"] or {}).get("proposal_id")) and not whole_review
    if any(demoted) or (proposal_cut and not every):
        assert "links.studio" in result["next"] and "skyvern_ember_answer" not in result["next"]
        assert "skyvern_ember_decide" not in result["next"]
    else:
        assert result["next"] == full["next"]
    assert len(kept) + result.get("user_actions_omitted", 0) == len(every)
    assert (len(kept) == len(every)) is all_fit
    assert (result["links"], result["progress"]["cursor"]) == (full["links"], full["progress"]["cursor"])
    assert "omitted" not in result["next"]
    assert all("prompt" in action for action in kept) is prompts_kept


def _sse(*frames: dict[str, Any]) -> bytes:
    return b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames)


TURN_START = {
    "type": "turn_start",
    "turn_id": TURN,
    "turn_index": 0,
    "timestamp": NOW.isoformat(),
    "ember_chat_id": CHAT,
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeEmber:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.requests: list[httpx.Request] = []
        self.chats: list[dict[str, Any]] = []
        self.histories: dict[str, list[dict[str, Any]]] = {}
        self.by_workflow: list[dict[str, Any]] = [_history(chat_id=None)]
        self.chat_post = httpx.Response(200, content=_sse(TURN_START))
        self.steer = httpx.Response(200, json={"steer_id": "s", "text": "t", "created_at": NOW.isoformat()})
        self.cancel = httpx.Response(204)
        self.debug_session = httpx.Response(404, json={"detail": "Not Found"})
        self.workflow_read = httpx.Response(
            200, json={"workflow_id": "w_1", "workflow_permanent_id": WPID, "title": "Invoices"}
        )
        self.workflow_create = httpx.Response(200, json={"workflow_id": "w_new", "workflow_permanent_id": WPID})
        self.browser_session = httpx.Response(200, json={"browser_session_id": "pbs_1", "status": "running"})
        self.delays: dict[str, float] = {}
        self.errors: dict[str, type[httpx.TransportError]] = {}
        self.on_steer: Callable[[httpx.Request], None] | None = None
        self.card_routes: dict[str, httpx.Response] = {}
        self.history_after_post: httpx.Response | httpx.HTTPError | None = None

    def set_history(self, chat_id: str, *payloads: dict[str, Any]) -> None:
        self.histories[chat_id] = list(payloads)

    def paths(self, suffix: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path.endswith(suffix)]

    def bodies(self, suffix: str) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.paths(suffix)]

    def _route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/")
        params = request.url.params
        if path == "v1/ember/chat-history":
            if "ember_chat_id" in params:
                queue = self.histories.get(params["ember_chat_id"]) or [_history(chat_id=None)]
            else:
                queue = self.by_workflow
            return httpx.Response(200, json=queue.pop(0) if len(queue) > 1 else queue[0])
        if path == "v1/ember/chats":
            return httpx.Response(200, json=self.chats)
        if path == "v1/ember/chat-post":
            return self.chat_post
        if path == "v1/ember/steer":
            return self.steer
        if path == "v1/ember/cancel":
            return self.cancel
        if path.removeprefix("v1/ember/") in self.card_routes:
            return self.card_routes[path.removeprefix("v1/ember/")]
        if path == f"api/v1/workflows/{WPID}":
            return self.workflow_read
        if path == "v1/workflows":
            return self.workflow_create
        if path.startswith("v1/debug-session/"):
            return self.debug_session
        if path.startswith("v1/browser_sessions/"):
            return self.browser_session
        if path == "v1/upload_file":
            return httpx.Response(200, json={"file_id": f"file_{len(self.paths('/upload_file')) - 1}"})
        return httpx.Response(404, json={"detail": "Not Found"})

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/")
        if path == "v1/ember/steer" and self.on_steer is not None:
            self.on_steer(request)
        posted = any(seen.method == "POST" for seen in self.requests)
        if path == "v1/ember/chat-history" and posted and self.history_after_post is not None:
            if isinstance(self.history_after_post, httpx.HTTPError):
                raise self.history_after_post
            return self.history_after_post
        for prefix, error in self.errors.items():
            if path.startswith(prefix):
                raise error("injected transport failure", request=request)
        for prefix, seconds in self.delays.items():
            if path.startswith(prefix):
                read_timeout = request.extensions["timeout"]["read"]
                if read_timeout is not None and seconds > read_timeout:
                    self.clock.now += read_timeout
                    raise httpx.ReadTimeout("read timed out", request=request)
                self.clock.now += seconds
        return self._route(request)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(ember, "time", SimpleNamespace(monotonic=fake.monotonic))
    monkeypatch.setattr(_ember_http, "time", SimpleNamespace(monotonic=fake.monotonic))
    monkeypatch.setattr(ember, "asyncio", ScopedAsyncio(sleep=fake.sleep))
    return fake


@pytest_asyncio.fixture
async def server(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> AsyncIterator[FakeEmber]:
    fake = FakeEmber(clock)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as base_client:
        wrapper = AsyncClientWrapper(
            api_key=API_KEY, base_url="https://api.example.test/api", timeout=60, httpx_client=base_client
        )
        client = SimpleNamespace(_client_wrapper=wrapper)
        monkeypatch.setattr(_ember_http, "get_skyvern", lambda: client)
        monkeypatch.setattr(_workflow_http, "get_skyvern", lambda: client)
        monkeypatch.setattr(_ember_http, "get_active_api_key", lambda: API_KEY)
        yield fake
    assert not [request for request in fake.requests if "/workflow/copilot" in request.url.path]


def _assert_no_secrets(result: dict[str, Any]) -> None:
    dumped = json.dumps(result)
    recovery = _ember_http.recovery_token()
    for secret in (CANCEL_TOKEN, RESUME_TOKEN, recovery):
        assert secret not in dumped


async def _call(tool: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
    result = await tool(**kwargs)
    _assert_no_secrets(result)
    return result


@pytest.mark.asyncio
async def test_chat_post_body_pins_flags_fresh_cancel_token_and_recovery_header(server: FakeEmber) -> None:
    server.set_history(CHAT, working_history())

    first = await _call(ember.skyvern_ember_send, message="Build it", workflow_id=WPID, wait_seconds=0)
    reads_after_first = len(server.paths("/chat-history"))
    second = await _call(ember.skyvern_ember_send, message="Build it", workflow_id=WPID, wait_seconds=0)
    checked = await _call(ember.skyvern_ember_check, ember_chat_id=CHAT, wait_seconds=0)
    offer = _finished_row(connectedAccountChoices=[{"connection_id": "goac_1", "name": "Work"}])
    server.set_history(OTHER_CHAT, _history(chat_id=OTHER_CHAT, messages=[_message("user", "Read my sheet"), offer]))
    picked = await _call(
        ember.skyvern_ember_send, ember_chat_id=OTHER_CHAT, connected_account_id="goac_1", wait_seconds=0
    )
    worded = await _call(
        ember.skyvern_ember_send,
        message="Use it",
        ember_chat_id=OTHER_CHAT,
        connected_account_id="goac_1",
        wait_seconds=0,
    )
    unoffered = await _call(
        ember.skyvern_ember_send, ember_chat_id=OTHER_CHAT, connected_account_id="goac_9", wait_seconds=0
    )

    assert first["ok"] and second["ok"] and checked["ok"] and picked["ok"], first
    # The server treats a pick as the user's click on an offered account, so only an offered one is sent.
    assert worded["error"]["code"] == unoffered["error"]["code"] == "INVALID_INPUT"
    assert 0 < reads_after_first < len(server.paths("/chat-history"))
    assert first["data"]["state"] == "working"
    assert first["data"]["delivered_as"] == "new_turn"
    *bodies, pick = server.bodies("/chat-post")
    # The server resumes a Test end-to-end only for a pick whose message is the account id, as the Studio sends it.
    assert (pick["message"], pick["selected_connected_account_id"]) == ("goac_1", "goac_1")
    assert pick["idempotency_key"]
    assert len(bodies) == 2
    # Every key sent is a field the server's request model reads, so a renamed field can't be dropped unseen.
    fields = WorkflowCopilotChatRequest.model_fields
    names = set(fields) | {
        choice
        for field in fields.values()
        if isinstance(field.validation_alias, AliasChoices)
        for choice in field.validation_alias.choices
    }
    for body in [*bodies, pick]:
        WorkflowCopilotChatRequest.model_validate(body)
        assert set(body) <= names, set(body) - names
    for body in bodies:
        assert {key: body[key] for key in EXPECTED_CHAT_POST_FLAGS} == EXPECTED_CHAT_POST_FLAGS
        assert "code_block" not in body and "audio_artifact_id" not in body and "demonstration_id" not in body
        assert "selected_connected_account_id" not in body and "idempotency_key" not in body
        assert re.fullmatch(r"[0-9a-f]{64}", body["credential_recovery_token"])
    assert bodies[0]["cancel_token"] != bodies[1]["cancel_token"]
    history_reads = server.paths("/chat-history")
    assert history_reads
    assert [read.headers.get(_ember_http.RECOVERY_HEADER) for read in history_reads] == [
        bodies[0]["credential_recovery_token"]
    ] * len(history_reads)


def _fill_every_chat_page(server: FakeEmber) -> None:
    server.chats = [
        {"ember_chat_id": f"wcc_old_{index}", "workflow_permanent_id": WPID, "in_flight": False}
        for index in range(_ember_http.CHATS_PAGE_SIZE)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "running", "steer_status", "delays", "code"),
    [
        ({"workflow_id": WPID}, "working", 200, {}, "API_ERROR"),
        ({"workflow_id": WPID, "new_chat": True}, "working", 200, {}, "API_ERROR"),
        ({"ember_chat_id": CHAT}, "idle", 200, {}, "API_ERROR"),
        ({"ember_chat_id": CHAT}, "working", 200, {}, "TURN_IN_PROGRESS"),
        ({"ember_chat_id": CHAT, "if_turn_running": "steer"}, "working", 409, {}, "API_ERROR"),
        ({"ember_chat_id": CHAT, "if_turn_running": "steer"}, "working", 200, {}, None),
        ({"ember_chat_id": CHAT, "if_turn_running": "steer"}, "working", 200, {"v1/ember/steer": 30.0}, None),
        ({"ember_chat_id": CHAT, "if_turn_running": "steer"}, "waiting", 200, {}, "TURN_IN_PROGRESS"),
        (
            {"ember_chat_id": CHAT, "if_turn_running": "steer", "action": "diagnose_run", "workflow_run_id": "wr_1"},
            "working",
            200,
            {},
            "INVALID_INPUT",
        ),
        ({"ember_chat_id": CHAT, "if_turn_running": "steer", "wait_seconds": 5}, "ends_on_steer", 200, {}, None),
        ({"ember_chat_id": CHAT, "if_turn_running": "steer", "wait_seconds": 5}, "pauses_on_steer", 200, {}, None),
        ({"ember_chat_id": CHAT, "if_turn_running": "steer", "wait_seconds": 5}, "ends_asking_on_steer", 200, {}, None),
        (
            {"ember_chat_id": CHAT, "if_turn_running": "steer", "wait_seconds": 5},
            "read_fails_after_steer",
            200,
            {},
            None,
        ),
        (
            {"ember_chat_id": CHAT, "if_turn_running": "steer", "wait_seconds": 5},
            "read_times_out_after_steer",
            200,
            {},
            None,
        ),
        ({"workflow_id": WPID, "wait_seconds": 45}, "idle", 200, {"v1/ember/chats": 9.0}, "RETRY_LATER"),
        ({"ember_chat_id": CHAT, "workflow_id": "wpid_another_workflow"}, "idle", 200, {}, "INVALID_INPUT"),
    ],
    ids=[
        "no_chat_id",
        "new_chat",
        "idle_chosen_chat",
        "refuse_on_running_chat",
        "steer_turn_ended",
        "steer_chosen",
        "steer_answer_lost",
        "steer_while_waiting_for_the_user",
        "structured_action_on_a_running_turn",
        "turn_ended_before_reading_the_steer",
        "turn_paused_before_reading_the_steer",
        "turn_ended_asking_the_user_before_reading_the_steer",
        "chat_read_fails_after_the_steer",
        "chat_read_times_out_after_the_steer",
        "slow_chat_pages_past_the_budget",
        "chat_on_another_workflow",
    ],
)
async def test_full_chat_scan_never_posts_a_new_turn(
    server: FakeEmber,
    clock: FakeClock,
    kwargs: dict[str, Any],
    running: str,
    steer_status: int,
    delays: dict[str, float],
    code: str | None,
) -> None:
    _fill_every_chat_page(server)
    idle = _history(messages=[_message("user", "Old"), _finished_row()])
    waiting = working_history() | {"question_interactions": [_question(status="pending")]}
    server.set_history(CHAT, idle if running == "idle" else waiting if running == "waiting" else working_history())
    if running == "ends_on_steer":
        server.on_steer = lambda request: server.set_history(CHAT, idle)
    if running == "pauses_on_steer":
        server.on_steer = lambda request: server.set_history(CHAT, waiting)
    if running == "ends_asking_on_steer":
        offer = _finished_row(connectedAccountChoices=[{"connection_id": "goac_1", "name": "Work"}])
        ended_asking = _history(messages=[_message("user", "Old"), offer])
        server.on_steer = lambda request: server.set_history(CHAT, ended_asking)
    if running == "read_fails_after_steer":
        server.on_steer = lambda request: server.errors.update({"v1/ember/chat-history": httpx.ReadError})
    if running == "read_times_out_after_steer":
        server.on_steer = lambda request: setattr(server, "delays", {"v1/ember/chat-history": 60.0})
    if steer_status == 409:
        server.steer = httpx.Response(409, json={"detail": "The Copilot turn has ended"})
    server.delays = delays
    call = {"wait_seconds": 0} | kwargs

    result = await _call(ember.skyvern_ember_send, message="Build it", **call)

    assert clock.now - 1000.0 <= max(call["wait_seconds"], ember.TURN_START_MIN_SECONDS)

    if code is None:
        assert result["ok"] and result["data"]["delivered_as"] == "steer" and len(server.paths("/steer")) == 1
    else:
        assert result["error"]["code"] == code
    if running == "waiting" or "action" in kwargs:
        # A steer queued behind the user's answer would sit unread, and one carrying an action would drop it.
        assert server.paths("/steer") == []
    if running == "working" and code is None and not delays:
        assert result["data"]["steer_status"] == "pending"
    if running == "pauses_on_steer":
        # A paused turn still holds the steer and reads it after the user answers, so resending would double it.
        assert result["data"]["steer_status"] == "pending" and "skyvern_ember_check" in result["data"]["next"]
    if running == "ends_asking_on_steer":
        # An ended turn that asks the user something holds no message; nothing will read the steer.
        assert (result["data"]["state"], result["data"]["steer_status"]) == ("waiting_for_user", "not_delivered")
    if running in ("read_fails_after_steer", "read_times_out_after_steer"):
        # Nothing was read after the steer, so the pre-steer read is not reported as the chat's state.
        assert (result["data"]["state"], result["data"]["steer_status"]) == ("unknown", "pending")
        assert "skyvern_ember_check" in result["data"]["next"]
    if running == "ends_on_steer":
        assert result["data"]["steer_status"] == "not_delivered"
    if "v1/ember/steer" in delays:
        # A steer whose answer was lost may have landed, so the caller is told to check, never to resend.
        assert result["data"]["steer_status"] == "unconfirmed"
        assert "skyvern_ember_check" in result["data"]["next"]
    assert server.paths("/chat-post") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"ember_chat_id": CHAT, "if_turn_running": "refuse"},
        {"new_chat": True, "if_turn_running": "steer"},
        {"ember_chat_id": OTHER_CHAT, "if_turn_running": "steer"},
    ],
    ids=["refuse", "new_chat", "different_chat"],
)
async def test_send_never_posts_while_a_chat_on_the_workflow_is_in_flight(
    server: FakeEmber, kwargs: dict[str, Any]
) -> None:
    server.chats = [{"ember_chat_id": CHAT, "workflow_permanent_id": WPID, "in_flight": True}]
    server.set_history(CHAT, working_history())
    server.set_history(OTHER_CHAT, _history(chat_id=OTHER_CHAT))

    result = await _call(ember.skyvern_ember_send, message="Also add a step", workflow_id=WPID, **kwargs)

    assert result["error"]["code"] == "TURN_IN_PROGRESS"
    assert result["data"]["ember_chat_id"] == CHAT and result["data"]["state"] == "working"
    assert server.paths("/chat-post") == [] and server.paths("/steer") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_ends_with_the_steer", [False, True], ids=["turn_running", "turn_ended"])
async def test_steer_on_the_running_chat_posts_steer_with_history_cancel_token(
    server: FakeEmber, turn_ends_with_the_steer: bool
) -> None:
    server.chats = [{"ember_chat_id": CHAT, "workflow_permanent_id": WPID, "in_flight": True}]
    delivered = working_history()
    server.set_history(CHAT, working_history(), delivered)

    def deliver(request: httpx.Request) -> None:
        steer_id = json.loads(request.content)["steer_id"]
        receipt = [{"steer_id": steer_id, "text": "t", "created_at": NOW.isoformat(), "delivered_at": NOW.isoformat()}]
        if turn_ends_with_the_steer:
            ended = _history(messages=[_message("user", "Build it"), _finished_row(steerMessages=receipt)])
            delivered.clear()
            delivered.update(ended)
        else:
            delivered["in_flight_turn"]["steer_messages"] = receipt

    server.on_steer = deliver

    first = await _call(
        ember.skyvern_ember_send, message="Use the second tab", if_turn_running="steer", workflow_id=WPID
    )

    assert first["ok"] and first["data"]["delivered_as"] == "steer" and first["data"]["steer_status"] == "delivered"
    # The receipt a later check reads is the one this send's steer_id names.
    assert (
        first["data"]["delivered_steer_ids"] == [first["data"]["steer_id"]] == [server.bodies("/steer")[0]["steer_id"]]
    )
    if turn_ends_with_the_steer:
        return
    second = await _call(ember.skyvern_ember_send, message="And the third", if_turn_running="steer", workflow_id=WPID)
    steers = server.bodies("/steer")
    assert [body["cancel_token"] for body in steers] == [CANCEL_TOKEN, CANCEL_TOKEN]
    assert steers[0]["steer_id"] != steers[1]["steer_id"]
    assert steers[0] == {**steers[0], "ember_chat_id": CHAT, "message": "Use the second tab"}
    assert second["ok"]
    assert server.paths("/chat-post") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "detail", "code"),
    [
        (409, "The Copilot turn has ended", None),
        (409, "This Copilot turn cannot take more messages", "TURN_IN_PROGRESS"),
        (503, "The safety screen is unavailable. Please retry your message.", "RETRY_LATER"),
    ],
    ids=["turn_ended", "turn_full", "safety_screen_down"],
)
async def test_a_refused_steer_starts_a_new_turn_only_when_the_turn_ended(
    server: FakeEmber, status_code: int, detail: str, code: str | None
) -> None:
    server.chats = [{"ember_chat_id": CHAT, "workflow_permanent_id": WPID, "in_flight": True}]
    server.set_history(CHAT, working_history())
    server.steer = httpx.Response(status_code, json={"detail": detail})

    result = await _call(ember.skyvern_ember_send, message="Add a step", if_turn_running="steer", workflow_id=WPID)

    if code is None:
        assert result["ok"] and result["data"]["delivered_as"] == "new_turn"
        assert len(server.paths("/chat-post")) == 1
    else:
        assert result["error"]["code"] == code
        assert server.paths("/chat-post") == []


@pytest.mark.asyncio
async def test_stale_in_flight_summary_reconciled_by_history_lets_send_start_a_turn(server: FakeEmber) -> None:
    server.chats = [{"ember_chat_id": CHAT, "workflow_permanent_id": WPID, "in_flight": True}]
    reconciled = _history(messages=[_message("user", "Old"), _finished_row()])
    server.set_history(CHAT, reconciled, reconciled, working_history())

    result = await _call(ember.skyvern_ember_send, message="Build it", workflow_id=WPID, wait_seconds=0)

    assert result["ok"], result
    assert len(server.paths("/chat-post")) == 1
    assert server.bodies("/chat-post")[0]["ember_chat_id"] == CHAT


@pytest.mark.asyncio
async def test_stream_drop_before_turn_start_recovers_by_cancel_token_without_reposting(server: FakeEmber) -> None:
    server.chat_post = httpx.Response(200, content=b": keep-alive\n\n" + _sse({"type": "processing_update"}))
    server.by_workflow = [_history(chat_id=None), _history(in_flight=_live(), request_turn_id=TURN)]

    result = await _call(ember.skyvern_ember_send, message="Build it", workflow_id=WPID, wait_seconds=30)

    assert result["ok"] and result["data"]["ember_chat_id"] == CHAT and result["data"]["state"] == "working"
    assert len(server.paths("/chat-post")) == 1
    token = server.bodies("/chat-post")[0]["cancel_token"]
    recovery_reads = [read for read in server.paths("/chat-history") if "request_cancel_token" in read.url.params]
    assert recovery_reads and {read.url.params["request_cancel_token"] for read in recovery_reads} == {token}


async def _unconfirmed_post(server: FakeEmber) -> dict[str, Any]:
    server.chat_post = httpx.Response(200, content=b": keep-alive\n\n")
    result = await _call(ember.skyvern_ember_send, message="Build it", workflow_id=WPID, wait_seconds=20)
    assert result["data"]["state"] == "submitted_unconfirmed" and "skyvern_ember_check" in result["data"]["next"]
    assert len(server.paths("/chat-post")) == 1
    return result


async def _slow_browser_start(server: FakeEmber) -> dict[str, Any]:
    server.chats = [{"ember_chat_id": CHAT, "workflow_permanent_id": WPID, "in_flight": False}]
    server.set_history(CHAT, working_history())
    server.debug_session = httpx.Response(200, json={"browser_session_id": "pbs_1"})
    server.browser_session = httpx.Response(200, json={"browser_session_id": "pbs_1", "status": "created"})
    server.delays = {"v1/debug-session/": 12.0, "v1/browser_sessions/": 9.0, "v1/ember/chat-post": 10.0}
    result = await _call(ember.skyvern_ember_send, message="Build it", workflow_id=WPID, wait_seconds=45)
    assert server.bodies("/chat-post")[0]["browser_session_id"] == "pbs_1"
    return result


async def _poll_check(server: FakeEmber) -> dict[str, Any]:
    server.set_history(CHAT, working_history())
    server.delays = {"v1/ember/chat-history": 8.0}
    return await _call(ember.skyvern_ember_check, ember_chat_id=CHAT, wait_seconds=12)


async def _poll_check_then_transcript(server: FakeEmber) -> dict[str, Any]:
    server.set_history(CHAT, working_history())
    server.delays = {"v1/ember/chat-history": 4.0}
    result = await _call(ember.skyvern_ember_check, ember_chat_id=CHAT, wait_seconds=45, transcript_messages=5)
    assert "transcript" in result["data"]
    return result


async def _slow_decide_accept(server: FakeEmber) -> dict[str, Any]:
    history = _recorded_history("finished_tested_proposal") | {"workflow_copilot_chat_id": CHAT}
    server.set_history(CHAT, history)
    server.card_routes["apply-proposed-workflow"] = httpx.Response(200, json={"workflow_id": "w_2", "version": 3})
    server.delays = {"v1/ember/chat-history": 9.0, "v1/ember/apply-proposed-workflow": 9.0}
    result = await _call(
        ember.skyvern_ember_decide,
        ember_chat_id=CHAT,
        item_id=_proposal_id(history),
        decision="accept",
        wait_seconds=20,
    )
    assert len(server.paths("/apply-proposed-workflow")) == 1
    return result


async def _history_changed_retry_after_turn_start(server: FakeEmber) -> dict[str, Any]:
    # Setup leaves 11 seconds; the first read takes 9 and gets the retryable 503, so the retry has 1 second left.
    server.delays = {f"api/v1/workflows/{WPID}": 9.5, "v1/ember/chats": 9.5}
    route = server._route
    changed = {"left": 1}

    def history_changed_once(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat-post"):
            server.delays["v1/ember/chat-history"] = 9.0
        elif path.endswith("/chat-history") and changed["left"]:
            changed["left"] -= 1
            server.delays["v1/ember/chat-history"] = 5.0
            return httpx.Response(503, json={"detail": _ember_http._HISTORY_CHANGED_DETAIL})
        return route(request)

    server._route = history_changed_once  # type: ignore[method-assign]
    result = await _call(ember.skyvern_ember_send, message="Build it", workflow_id=WPID, wait_seconds=30)
    assert len(server.paths("/chat-post")) == 1 and result["data"]["ember_chat_id"] == CHAT
    return result


async def _poll_stop(server: FakeEmber) -> dict[str, Any]:
    server.set_history(CHAT, working_history())
    server.delays = {"v1/ember/chat-history": 4.0}
    return await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=5)


async def _poll_recovery(server: FakeEmber) -> dict[str, Any]:
    server.chat_post = httpx.Response(200, content=b": keep-alive\n\n")
    server.delays = {"v1/ember/chat-post": 18.0, "v1/ember/chat-history": 8.0}
    return await _call(ember.skyvern_ember_send, message="Build it", wait_seconds=20)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "wait_seconds"),
    [
        (_poll_check, 12),
        (_poll_check_then_transcript, 45),
        (_slow_decide_accept, 20),
        (_history_changed_retry_after_turn_start, 30),
        (_poll_stop, 5),
        (_poll_recovery, 20),
        (_unconfirmed_post, 20),
        (_slow_browser_start, 45),
    ],
    ids=[
        "check_watch",
        "check_then_transcript",
        "decide_accept_read_back",
        "send_history_retry_after_turn_start",
        "stop_loop",
        "send_recovery",
        "send_unconfirmed",
        "send_slow_browser",
    ],
)
async def test_slow_history_polls_stop_at_the_deadline(
    server: FakeEmber,
    clock: FakeClock,
    scenario: Callable[[FakeEmber], Any],
    wait_seconds: int,
) -> None:
    result = await scenario(server)

    assert result["ok"], result
    assert clock.now - 1000.0 <= wait_seconds + 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shape", "reads", "summarized"),
    [("owner_past_the_short_read", 2, True), ("owner_in_the_short_read", 1, True), ("owner_turn_running", 1, False)],
    ids=["owner_past_the_short_read", "owner_in_the_short_read", "owner_turn_running"],
)
async def test_check_reads_the_whole_chat_only_when_the_proposal_owner_row_is_past_the_short_read(
    server: FakeEmber, shape: str, reads: int, summarized: bool
) -> None:
    history = _recorded_history("finished_tested_proposal")
    chat_id = history["workflow_copilot_chat_id"]
    owner_turn_id = history["proposed_workflow_metadata"]["owner_turn_id"]
    short = history | {"chat_history": [row for row in history["chat_history"] if row["sender"] != "ai"]}
    payloads = {
        "owner_past_the_short_read": (short, history),
        "owner_in_the_short_read": (history,),
        "owner_turn_running": (short | {"in_flight_turn": _live() | {"turn_id": owner_turn_id}},),
    }[shape]
    server.set_history(chat_id, *payloads)

    checked = await _call(ember.skyvern_ember_check, ember_chat_id=chat_id, wait_seconds=0)

    short_reads = ["message_limit" in read.url.params for read in server.paths("/chat-history")]
    assert short_reads == [True] + [False] * (reads - 1)
    full_changes = ember.map_turn_state(history, now=NOW)["proposal"]["changes"]
    assert full_changes and checked["data"]["proposal"]["changes"] == (full_changes if summarized else None)


@pytest.mark.asyncio
async def test_error_mapping_by_route(server: FakeEmber, monkeypatch: pytest.MonkeyPatch) -> None:
    server.set_history(CHAT, _history(chat_id=None))
    unknown = await _call(ember.skyvern_ember_check, ember_chat_id=CHAT, wait_seconds=0)
    assert unknown["error"]["code"] == "CHAT_NOT_FOUND"
    unknown_stop = await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=0)
    assert unknown_stop["error"]["code"] == "CHAT_NOT_FOUND" and server.paths("/cancel") == []
    assert "upgrade" not in json.dumps(unknown["error"]).lower()

    server.set_history(CHAT, working_history())
    server.cancel = httpx.Response(404, json={"detail": "Unknown Copilot chat"})
    handler_404 = await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=0)
    assert handler_404["error"]["code"] == "CHAT_NOT_FOUND"
    assert "upgrade" not in json.dumps(handler_404["error"]).lower()

    server.cancel = httpx.Response(404, json={"detail": "Not Found"})
    route_miss = await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=0)
    assert route_miss["error"]["code"] == "EMBER_UNAVAILABLE"

    server.cancel = httpx.Response(503, json={"detail": "Cancel not supported in this environment"})
    no_cache = await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=0)
    assert no_cache["error"]["code"] == "API_ERROR"

    server.cancel = await _server_turn_control_response(monkeypatch, None, "cancel")
    role_check = await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=0)
    assert role_check["error"]["code"] == "RETRY_LATER" and role_check["error"]["details"]["retry_after"] == "2"

    server.cancel = await _server_turn_control_response(monkeypatch, False, "cancel")
    denied = await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=0)
    assert denied["error"]["code"] == "TURN_CONTROL_DENIED"
    # The server's own turn-control refusals, for steer as well as cancel, map to the codes the agent acts on.
    for control in ("cancel", "steer"):
        for allowed, status_code, code in ((False, 403, "TURN_CONTROL_DENIED"), (None, 503, "RETRY_LATER")):
            response = await _server_turn_control_response(monkeypatch, allowed, control)
            error = _ember_http.classify_error(response, control)
            assert (response.status_code, error.code) == (status_code, code)

    server.cancel = httpx.Response(403, json={"detail": "Missing permission"})
    forbidden = await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=0)
    assert forbidden["error"]["code"] == "PERMISSION_DENIED" and "workflow_copilot" in forbidden["error"]["message"]
    # A turn whose cancel token the server hides from this caller is not theirs to stop, so no cancel is sent.
    cancels = len(server.paths("/cancel"))
    server.set_history(CHAT, _history(in_flight={**_live(), "cancel_token": None}))
    hidden = await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=0)
    assert hidden["error"]["code"] == "TURN_CONTROL_DENIED" and len(server.paths("/cancel")) == cancels

    old_server = working_history()
    del old_server["app_url"]
    server.set_history(CHAT, old_server)
    missing_key = await _call(ember.skyvern_ember_check, ember_chat_id=CHAT, wait_seconds=0)
    assert missing_key["error"]["code"] == "EMBER_UNAVAILABLE"

    server.chat_post = httpx.Response(404, json={"detail": f"{_ember_http._RUN_NOT_FOUND_PREFIX} of this workflow."})
    no_run = await _call(
        ember.skyvern_ember_send, message="Why?", workflow_id=WPID, action="diagnose_run", workflow_run_id="wr_9"
    )
    assert no_run["error"]["code"] == "RUN_NOT_FOUND"

    server.chat_post = httpx.Response(404, json={"detail": _ember_http._WORKFLOW_NOT_FOUND_DETAIL})
    no_workflow = await _call(ember.skyvern_ember_send, message="Build it", workflow_id=WPID, wait_seconds=0)
    assert no_workflow["error"]["code"] == "WORKFLOW_NOT_FOUND"
    # A turn another client started after send's own check is refused by the server, never stopped.
    refused = {"type": "error", "error": workflow_copilot.COPILOT_TURN_RUNNING_REFUSED}
    server.chat_post = httpx.Response(200, content=_sse(refused))
    raced = await _call(ember.skyvern_ember_send, message="Build it", workflow_id=WPID, wait_seconds=0)
    assert raced["error"]["code"] == "TURN_IN_PROGRESS"

    # Transport failures and workflow-route errors come back as tool results the agent can act on, not exceptions.
    server.workflow_create = httpx.Response(403, json={"detail": "Missing permission"})
    create_refused = await _call(ember.skyvern_ember_send, message="Build it", wait_seconds=0)
    server.workflow_read = httpx.Response(500, json={"detail": "boom"})
    read_failed = await _call(ember.skyvern_ember_send, message="Build it", workflow_id=WPID, wait_seconds=0)
    assert create_refused["error"]["code"] == read_failed["error"]["code"] == "API_ERROR"
    server.set_history(CHAT, working_history())
    server.delays = {"v1/ember/chats": 30.0, "v1/ember/cancel": 30.0}
    slow_list = await _call(ember.skyvern_ember_list, workflow_id=WPID)
    slow_cancel = await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=0)
    assert slow_list["error"]["code"] == "RETRY_LATER"
    assert slow_cancel["error"]["code"] == "API_ERROR" and "may have taken effect" in slow_cancel["error"]["message"]
    server.delays = {}
    server.errors = {"v1/ember/chat-history": httpx.ReadError}
    unread = await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=0)
    assert unread["error"]["code"] == "API_ERROR"
    # A read that times out after the cancel leaves the state unknown rather than the running state read before it.
    server.errors, server.cancel = {}, httpx.Response(204)
    route = server._route

    def slow_history_after_cancel(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancel"):
            server.delays = {"v1/ember/chat-history": 60.0}
        return route(request)

    server._route = slow_history_after_cancel  # type: ignore[method-assign]
    unread_after_cancel = await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=5)
    assert unread_after_cancel["ok"] and unread_after_cancel["data"]["state"] == "unknown", unread_after_cancel
    server._route = route  # type: ignore[method-assign]
    server.delays = {}
    # A steer whose connection broke after sending may have landed, so it is reported like a lost answer.
    server.errors = {"v1/ember/steer": httpx.ReadError}
    server.workflow_read = httpx.Response(200, json={"workflow_id": "w_1", "workflow_permanent_id": WPID})
    server.chats = [{"ember_chat_id": CHAT, "workflow_permanent_id": WPID, "in_flight": True}]
    steered = await _call(
        ember.skyvern_ember_send, message="Also", ember_chat_id=CHAT, if_turn_running="steer", wait_seconds=0
    )
    assert steered["ok"] and steered["data"]["steer_status"] == "unconfirmed", steered
    # An accepted steer whose body isn't JSON is still a delivered steer, not a crash.
    server.errors = {}
    server.steer = httpx.Response(200, content=b"accepted")
    plain = await _call(
        ember.skyvern_ember_send, message="Also", ember_chat_id=CHAT, if_turn_running="steer", wait_seconds=0
    )
    assert plain["ok"] and plain["data"]["delivered_as"] == "steer", plain
    # A connection that never opened sent nothing, so send says so at once instead of looking for a started turn.
    server.chats = []
    server.errors = {"v1/ember/chat-post": httpx.ConnectTimeout}
    unreached = await _call(ember.skyvern_ember_send, message="Build it", workflow_id=WPID, wait_seconds=30)
    assert unreached["error"]["code"] == "API_ERROR", unreached
    server.errors = {}
    server.workflow_read = httpx.Response(500, json={"detail": "boom"})
    proposal = _recorded_history("finished_tested_proposal")
    server.set_history(CHAT, proposal | {"workflow_permanent_id": WPID, "workflow_copilot_chat_id": CHAT})
    diff_unread = await _call(ember.skyvern_ember_check, ember_chat_id=CHAT, proposal_detail="diff", wait_seconds=0)
    assert diff_unread["error"]["code"] == "API_ERROR"


async def _server_turn_control_response(
    monkeypatch: pytest.MonkeyPatch, allowed: bool | None, control: CopilotTurnControl
) -> httpx.Response:
    check = AsyncMock(return_value=allowed) if allowed is not None else AsyncMock(side_effect=RuntimeError("down"))
    monkeypatch.setattr(
        workflow_copilot, "app", SimpleNamespace(AGENT_FUNCTION=SimpleNamespace(copilot_turn_control_allowed=check))
    )
    turn = CopilotPendingTurn(turn_id=TURN, started_at=NOW, starter_user_id="user_1")
    with pytest.raises(HTTPException) as raised:
        await workflow_copilot._require_copilot_turn_control("o_1", turn, control)
    return httpx.Response(raised.value.status_code, json={"detail": raised.value.detail}, headers=raised.value.headers)


@pytest.mark.asyncio
async def test_stop_with_claimed_deletion_cancels_and_reports_deletion_in_progress(server: FakeEmber) -> None:
    claimed = _question(
        credential_delete_review={
            "rows": [{"credential_id": "cred_1", "name": "Main", "credential_type": "password"}],
            "total_credential_count": 1,
            "claimed_at": NOW.isoformat(),
            "approved_credential_ids": ["cred_1"],
        }
    )
    running = _history(in_flight=_live(), question_interactions=[claimed])
    stopped = _history(
        messages=[
            _message("user", "Build it"),
            _message("ai", "Stopped.", turn_outcome={"response_kind": "recover", "terminal_reason": "cancel"}),
        ]
    )
    server.set_history(CHAT, running, stopped)

    result = await _call(ember.skyvern_ember_stop, ember_chat_id=CHAT, wait_seconds=10)

    assert result["ok"] and result["data"]["state"] == "stopped"
    assert result["data"]["deletion_in_progress"] is True
    assert server.bodies("/cancel") == [{"ember_chat_id": CHAT, "cancel_token": CANCEL_TOKEN, "source": "api"}]


@pytest.fixture
def attach_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(ember, "is_stdio_local_file_access_enabled", lambda: True)
    return work


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "names", "delays", "wait_seconds", "uploads", "code"),
    [
        ({"workflow_id": WPID}, ["invoices.csv"], {}, 45, 1, None),
        ({"workflow_id": WPID}, ["a.csv", "b.csv", "c.csv"], {"v1/upload_file": 25.0}, 45, 2, "RETRY_LATER"),
        ({"workflow_id": WPID}, ["invoices.csv"], {f"api/v1/workflows/{WPID}": 1.0}, 16, 0, "RETRY_LATER"),
        ({"workflow_id": WPID}, ["invoices.csv"], {}, 15, 0, "INVALID_INPUT"),
        ({}, ["invoices.csv"], {"v1/workflows": 1.0}, 16, 0, "RETRY_LATER"),
        (
            {"workflow_id": WPID},
            ["invoices.csv"],
            {"v1/debug-session/": 12.0, "v1/browser_sessions/": 9.0},
            45,
            1,
            None,
        ),
        ({"workflow_id": WPID}, [], {f"api/v1/workflows/{WPID}": 9.5, "v1/ember/chats": 5.0}, 0, 0, "RETRY_LATER"),
        (
            {"ember_chat_id": CHAT},
            [],
            {"v1/ember/chat-history": 9.0, f"api/v1/workflows/{WPID}": 9.5},
            0,
            0,
            "RETRY_LATER",
        ),
    ],
    ids=[
        "one_file",
        "slow_files_past_the_upload_budget",
        "file_with_no_time_left",
        "file_with_wait_too_short_for_uploads",
        "file_after_creating_a_workflow",
        "slow_browser_after_the_file",
        "setup_reads_used_the_budget",
        "slow_workflow_read",
    ],
)
async def test_send_fits_setup_uploads_and_the_turn_start_in_the_call_deadline(
    server: FakeEmber,
    clock: FakeClock,
    attach_dir: Path,
    target: dict[str, str],
    names: list[str],
    delays: dict[str, float],
    wait_seconds: int,
    uploads: int,
    code: str | None,
) -> None:
    server.set_history(CHAT, working_history())
    server.debug_session = httpx.Response(200, json={"browser_session_id": "pbs_1"})
    server.browser_session = httpx.Response(200, json={"browser_session_id": "pbs_1", "status": "created"})
    for name in names:
        (attach_dir / name).write_bytes(b"id,total\n1,10\n")
    server.delays = delays

    result = await _call(
        ember.skyvern_ember_send, message="Use this", file_paths=names, wait_seconds=wait_seconds, **target
    )

    assert clock.now - 1000.0 <= max(wait_seconds, ember.TURN_START_MIN_SECONDS) + 2
    assert len(server.paths("/upload_file")) == uploads
    if code is not None:
        assert result["error"]["code"] == code and server.paths("/chat-post") == []
        # A refusal after the blank workflow exists names it, so a retry reuses it instead of making another.
        assert (f"workflow_id={WPID}" in result["error"]["message"]) is bool(server.paths("/v1/workflows"))
        return
    assert result["ok"], result
    assert b"id,total" in server.paths("/upload_file")[0].content
    assert server.bodies("/chat-post")[0]["attached_file_ids"] == ["file_0"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "outside_root",
        "symlink",
        "oversize",
        "skyvern_home",
        "hidden",
        "cwd_is_root",
        "cwd_is_home",
        "cwd_above_home",
        "null_byte",
        "http_transport",
        "running_turn",
    ],
)
async def test_send_refuses_unsafe_attachments_and_posts_nothing(
    server: FakeEmber, attach_dir: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    outside = attach_dir.parent / "secret.txt"
    outside.write_text("secret")
    home = attach_dir.parent / "home"
    (home / ".skyvern").mkdir(parents=True)
    (home / ".skyvern" / ".env").write_text("SKYVERN_API_KEY=secret")
    monkeypatch.setenv("HOME", str(home))
    (attach_dir / "invoices.csv").write_text("id\n1\n")
    path = "invoices.csv"
    send_kwargs: dict[str, Any] = {}
    if case == "http_transport":
        monkeypatch.setattr(ember, "is_stdio_local_file_access_enabled", lambda: False)
    elif case == "running_turn":
        server.chats = [{"ember_chat_id": CHAT, "workflow_permanent_id": WPID, "in_flight": True}]
        server.set_history(CHAT, working_history())
        send_kwargs = {"workflow_id": WPID, "if_turn_running": "steer"}
    elif case == "outside_root":
        path = str(outside)
    elif case == "skyvern_home":
        path = "~/.skyvern/.env"
    elif case == "hidden":
        (attach_dir / ".env").write_text("SECRET=1")
        path = ".env"
    elif case == "cwd_is_root":
        monkeypatch.chdir("/")
        path = "etc/hosts"
    elif case == "cwd_is_home":
        monkeypatch.setenv("HOME", str(attach_dir))
        (attach_dir / "notes.txt").write_text("ok")
        path = "notes.txt"
    elif case == "cwd_above_home":
        (attach_dir / "me" / "Documents").mkdir(parents=True)
        (attach_dir / "me" / "Documents" / "taxes.pdf").write_text("private")
        monkeypatch.setenv("HOME", str(attach_dir / "me"))
        path = "me/Documents/taxes.pdf"
    elif case == "null_byte":
        path = "notes\x00.txt"
    elif case == "symlink":
        (attach_dir / "link.txt").symlink_to(attach_dir / "real.txt")
        (attach_dir / "real.txt").write_text("ok")
        path = "link.txt"
    elif case == "oversize":
        monkeypatch.setattr(ember, "MAX_ATTACHED_FILE_BYTES", 10)
        (attach_dir / "big.txt").write_bytes(b"x" * 11)
        path = "big.txt"

    result = await _call(ember.skyvern_ember_send, message="Use this", file_paths=[path], **send_kwargs)

    assert result["error"]["code"] == "INVALID_INPUT"
    assert _posts(server) == []


# Synthesized in the NarrativeCreatedTrigger shape: only a signed-in Studio user with Gmail connected can get Ember to
# create a Gmail trigger, and no API key can, so no MCP-driven run records one.
SYNTHESIZED_GMAIL_TRIGGER: NarrativeCreatedTrigger = {
    "eventTriggerId": "et_gmail",
    "sourceKind": "gmail",
    "filters": [{"label": "From", "value": "billing@example.test"}],
    "firesOnItsOwn": True,
    "connectionId": "cred_google_1",
}


def _created_triggers_history() -> dict[str, Any]:
    history = _recorded_history("created_trigger_webhook")
    history["chat_history"][-1]["narrative_payload"]["createdTriggers"].append(SYNTHESIZED_GMAIL_TRIGGER)
    return history


@pytest.mark.asyncio
async def test_no_registered_tool_input_schema_names_or_holds_a_server_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ember_http, "get_active_api_key", lambda: API_KEY)
    names = {"resume_token", "cancel_token", "credential_recovery_token", "owner_turn_id", "revision"}
    values = {CANCEL_TOKEN, RESUME_TOKEN, _ember_http.recovery_token()}
    for case in RECORDED_MAPPER_CASES:
        values |= _recorded_token_values(_recorded_history(case))
    tools = await mcp.list_tools(run_middleware=False)

    assert {"skyvern_ember_answer", "skyvern_ember_decide"} <= {tool.name for tool in tools}
    for tool in tools:
        schema = json.dumps(tool.parameters)
        assert not [name for name in names if f'"{name}"' in schema], tool.name
        assert not [value for value in values if value in schema], tool.name


def _posts(server: FakeEmber) -> list[httpx.Request]:
    return [request for request in server.requests if request.method == "POST"]


async def _call_recorded(tool: Callable[..., Any], history: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    result = await _call(tool, **kwargs)
    dumped = json.dumps(result)
    assert not [token for token in _recorded_token_values(history) if token in dumped]
    assert not [name for name in ("resume_token", "cancel_token", "credential_recovery_token") if name in dumped]
    return result


def _with_card(history: dict[str, Any], **card_extra: Any) -> dict[str, Any]:
    history = json.loads(json.dumps(history))
    history["pending_credential_requests"][0].update(card_extra)
    return history


def _login_item_id(history: dict[str, Any]) -> str:
    return ember._login_item_id(history["pending_credential_requests"][0])


RECORDED_SITE_QUESTION = _recorded_history("site_question")["pending_credential_requests"][0]["site_question"]
RECORDED_SITE_CREDENTIAL = RECORDED_SITE_QUESTION["credential_id"]


def _deletion_with_long_names() -> dict[str, Any]:
    history = _deletion_history()
    review = history["question_interactions"][0]["credential_delete_review"]
    review["rows"] = [
        {"credential_id": f"cred_{index}", "name": f"{index:02d} " + "n" * 200, "credential_type": "password"}
        for index in range(30)
    ]
    return history


def _deletion_history() -> dict[str, Any]:
    review = {
        "rows": [
            {"credential_id": "cred_1", "name": "Main", "credential_type": "password"},
            {"credential_id": "cred_2", "name": "Spare", "credential_type": "password"},
        ],
        "total_credential_count": 2,
    }
    return _history(
        in_flight=_live(pausedFor="question"),
        question_interactions=[_question(interaction_id="q_delete", credential_delete_review=review)],
    )


def _proposal_id(history: dict[str, Any]) -> str:
    metadata = history["proposed_workflow_metadata"]
    return f"{metadata['owner_turn_id']}:{metadata['revision']}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "kwargs", "message_part"),
    [
        ("question", {"item_id": "q_1", "turn_id": TURN, "text": "CSV"}, "exactly one of item_id"),
        ("run_approval", {"item_id": "4f0b28615bb745dfa18df47613a70c38", "text": "yes"}, "skyvern_ember_decide"),
        ("login_card", {"item_id": "login:c913bc19c3e24867beef6ff6af1f9340", "text": "yes"}, "skyvern_ember_decide"),
        (
            "finished_untested_proposal",
            {"item_id": "5bfdf5e10c1b494ea66b44186ee02eaf:1", "text": "accept"},
            "skyvern_ember_decide",
        ),
        (
            "question_with_six_parts",
            {
                "item_id": _question_with_six_parts()["question_interactions"][0]["interaction_id"],
                "answers": [ember.EmberQuestionAnswer(part_id="part_0", text="CSV")],
            },
            "part_5",
        ),
        (
            "question_with_six_parts",
            {
                "item_id": _question_with_six_parts()["question_interactions"][0]["interaction_id"],
                "answers": [ember.EmberQuestionAnswer(part_id="part_0", text="CSV")],
                "text": "and the rest as before",
            },
            "part_5",
        ),
        (
            "question_with_a_detail_choice",
            {
                "item_id": "e8b974fb985a478db54a07040689c96b",
                "answers": [
                    ember.EmberQuestionAnswer(
                        part_id="af878b4789b540729b4d6edc2fd17fb9", choice_id="17266de79db948abaa42b9dbdd1b14b1"
                    )
                ],
            },
            "Which account number?",
        ),
        (
            "question",
            {
                "item_id": "e8b974fb985a478db54a07040689c96b",
                "answers": [ember.EmberQuestionAnswer(part_id="af878b4789b540729b4d6edc2fd17fb9")],
            },
            "needs a choice_id or text",
        ),
        ("question", {"item_id": "connect:123", "text": "yes"}, "skyvern_ember_send"),
        (partial(_many_questions_history, 300), {"item_id": "q_0", "text": "CSV"}, "too large to show in full"),
    ],
    ids=[
        "mixed_ids",
        "run_approval",
        "login_card",
        "proposal",
        "unanswered_part",
        "unanswered_part_beside_text",
        "choice_without_its_detail",
        "empty_part_answer",
        "account_choice_id_shaped_like_a_proposal",
        "question_too_large_to_show",
    ],
)
async def test_answer_refusals_post_nothing(
    server: FakeEmber, case: str | Callable[[], dict[str, Any]], kwargs: dict[str, Any], message_part: str
) -> None:
    if callable(case):
        history = case()
    else:
        history = EDITED_MAPPER_CASES[case][0]() if case in EDITED_MAPPER_CASES else _recorded_history(case)
    chat_id = history["workflow_copilot_chat_id"]
    server.set_history(chat_id, history)

    result = await _call_recorded(ember.skyvern_ember_answer, history, ember_chat_id=chat_id, **kwargs)

    assert result["error"]["code"] == "INVALID_INPUT" and message_part in result["error"]["message"]
    assert _posts(server) == []


@pytest.mark.asyncio
async def test_late_answer_reports_the_record_the_server_returned(server: FakeEmber) -> None:
    history = _recorded_history("question")
    chat_id = history["workflow_copilot_chat_id"]
    pending = next(item for item in history["question_interactions"] if item["status"] == "pending")
    stored = pending | {"status": "resolved", "response": {"answers": [], "text": "JSON", "skipped": False}}
    server.set_history(chat_id, history, _recorded_history("working"))
    server.card_routes["question-response"] = httpx.Response(200, json=stored)

    result = await _call_recorded(
        ember.skyvern_ember_answer, history, ember_chat_id=chat_id, item_id=pending["interaction_id"], text="CSV"
    )

    assert result["ok"], result
    assert result["data"]["answer_recorded"] == {"status": "resolved", "response": stored["response"]}
    assert server.bodies("/question-response") == [
        {
            "ember_chat_id": chat_id,
            "interaction_id": pending["interaction_id"],
            "answers": [],
            "text": "CSV",
            "skipped": False,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_auto_accept", "requested_auto_accept"),
    [(False, False), (True, False), (False, True)],
    ids=["off", "already_on", "turned_on_with_this_accept"],
)
async def test_accept_and_reject_send_reviewed_tokens_and_keep_auto_accept(
    server: FakeEmber, current_auto_accept: bool, requested_auto_accept: bool
) -> None:
    history = _recorded_history("finished_tested_proposal") | {"auto_accept": current_auto_accept}
    chat_id = history["workflow_copilot_chat_id"]
    metadata = history["proposed_workflow_metadata"]
    past_the_owner_turn = history | {"chat_history": [row for row in history["chat_history"] if row["sender"] != "ai"]}
    server.set_history(chat_id, past_the_owner_turn, history)
    server.card_routes["apply-proposed-workflow"] = httpx.Response(200, json={"workflow_id": "w_2", "version": 3})
    # An accepted write whose body is not JSON still reports the write, not a crash.
    server.card_routes["clear-proposed-workflow"] = httpx.Response(200, content=b"cleared")
    reviewed = {"ember_chat_id": chat_id, "owner_turn_id": metadata["owner_turn_id"], "revision": metadata["revision"]}

    checked = await _call_recorded(ember.skyvern_ember_check, history, ember_chat_id=chat_id, wait_seconds=0)
    accepted = await _call_recorded(
        ember.skyvern_ember_decide,
        history,
        ember_chat_id=chat_id,
        item_id=_proposal_id(history),
        decision="accept",
        auto_accept=requested_auto_accept,
    )
    rejected = await _call_recorded(
        ember.skyvern_ember_decide, history, ember_chat_id=chat_id, item_id=_proposal_id(history), decision="reject"
    )

    reviewed_changes = ember.map_turn_state(history, now=NOW)["proposal"]["changes"]
    assert reviewed_changes and checked["data"]["proposal"]["changes"] == reviewed_changes
    assert accepted["ok"] and rejected["ok"], (accepted, rejected)
    applied_auto_accept = current_auto_accept or requested_auto_accept
    assert server.bodies("/apply-proposed-workflow") == [reviewed | {"auto_accept": applied_auto_accept}]
    assert server.bodies("/clear-proposed-workflow") == [reviewed | {"auto_accept": current_auto_accept}]
    assert accepted["data"]["outcome"] == {"accepted": True, "version": 3, "auto_accept": applied_auto_accept}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "item_id", "decision", "code"),
    [
        pytest.param("new_revision", None, "accept", "PROPOSAL_CHANGED", id="new_revision"),
        pytest.param("no_metadata", None, "accept", "INVALID_INPUT", id="no_metadata"),
        pytest.param("no_proposal", None, "accept", "NO_PROPOSAL", id="no_proposal"),
        *[
            pytest.param(None, None, decision, "INVALID_INPUT", id=f"{decision}_on_a_proposal")
            for decision in ("approve", "decline", "skip")
        ],
        *[
            pytest.param(None, item_id, decision, "ITEM_EXPIRED", id=f"{decision}_on_{item_id}")
            for item_id in ("not-a-proposal", "turn-1:", ":3", "turn-1:latest")
            for decision in ("accept", "reject")
        ],
    ],
)
async def test_accept_on_a_changed_or_missing_draft_posts_nothing(
    server: FakeEmber, change: str | None, item_id: str | None, decision: str, code: str
) -> None:
    history = _recorded_history("finished_tested_proposal")
    chat_id = history["workflow_copilot_chat_id"]
    reviewed = _proposal_id(history)
    if change == "new_revision":
        history["proposed_workflow_metadata"]["revision"] += 1
    elif change == "no_metadata":
        history["proposed_workflow_metadata"] = None
    elif change == "no_proposal":
        history["proposed_workflow"] = None
    server.set_history(chat_id, history)

    result = await _call_recorded(
        ember.skyvern_ember_decide, history, ember_chat_id=chat_id, item_id=item_id or reviewed, decision=decision
    )

    assert result["error"]["code"] == code
    assert _posts(server) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "site_answer", "response"),
    [
        ("login_card", None, {"result": "site_question", "site_question": RECORDED_SITE_QUESTION}),
        ("site_question", "add", {"result": "accepted"}),
    ],
    ids=["first_pick", "site_answer_add"],
)
async def test_saved_login_pick_asks_site_and_relays_the_site_question(
    server: FakeEmber, case: str, site_answer: str | None, response: dict[str, Any]
) -> None:
    history = _recorded_history(case)
    chat_id = history["workflow_copilot_chat_id"]
    card = history["pending_credential_requests"][0]
    server.set_history(chat_id, history)
    server.card_routes["credential-response"] = httpx.Response(200, json=response)
    answer = {"site_answer": site_answer} if site_answer else {}

    result = await _call_recorded(
        ember.skyvern_ember_decide,
        history,
        ember_chat_id=chat_id,
        item_id=_login_item_id(history),
        decision="use_saved_login",
        credential_ids=[RECORDED_SITE_CREDENTIAL],
        **answer,
    )

    assert result["ok"], result
    assert server.bodies("/credential-response") == [
        {
            "turn_id": card["turn_id"],
            "ember_chat_id": chat_id,
            "resume_token": card["resume_token"],
            "action": "connected",
            "credential_id": RECORDED_SITE_CREDENTIAL,
            "asks_site": True,
            **answer,
        }
    ]
    if response["result"] != "site_question":
        assert result["data"]["outcome"] == response
        return
    relayed = result["data"]["outcome"]["site_question"]
    assert relayed["site"] == RECORDED_SITE_QUESTION["site"]
    assert relayed["saved_sites"] == RECORDED_SITE_QUESTION["saved_sites"]
    assert "every saved run that uses it" in relayed["consent"]
    assert "site_answer='add'" in result["data"]["next"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("card_extra", "kwargs", "response"),
    [
        ({}, {"decision": "use_saved_login", "credential_ids": ["cred_portal"]}, {"result": "accepted"}),
        ({"signing_in": True}, {"decision": "signed_in"}, {"result": "signed_in"}),
    ],
    ids=["use_saved_login", "signed_in"],
)
async def test_a_resolved_login_card_reads_as_working_only_in_the_deciding_call(
    server: FakeEmber, clock: FakeClock, card_extra: dict[str, Any], kwargs: dict[str, Any], response: dict[str, Any]
) -> None:
    history = _with_card(_recorded_history("login_card"), **card_extra)
    chat_id = history["workflow_copilot_chat_id"]
    server.set_history(chat_id, history, history | {"pending_credential_requests": []})
    server.card_routes["credential-response"] = httpx.Response(200, json=response)

    result = await _call_recorded(
        ember.skyvern_ember_decide, history, ember_chat_id=chat_id, item_id=_login_item_id(history), **kwargs
    )

    assert (result["data"]["state"], result["data"]["waiting_on"]) == ("working", None)
    assert "skyvern_ember_check" in result["data"]["next"]
    assert result["data"]["outcome"]["result"] == response["result"]
    before = clock.now
    checked = await _call_recorded(ember.skyvern_ember_check, history, ember_chat_id=chat_id, wait_seconds=30)
    assert (checked["data"]["state"], checked["data"]["waiting_on"]) == (
        "waiting_for_user",
        "the person who started this turn",
    )
    assert clock.now == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("browser_session", "live_browser_url", "next_part"),
    [
        (
            httpx.Response(200, json={"app_url": "https://app.example.test/browser/pbs_1"}),
            "https://app.example.test/browser/pbs_1",
            "outcome.live_browser_url",
        ),
        (httpx.Response(404, json={"detail": "Not Found"}), None, "links.studio"),
    ],
    ids=["live_browser", "no_live_browser"],
)
async def test_sign_in_myself_returns_the_live_browser_link(
    server: FakeEmber, browser_session: httpx.Response, live_browser_url: str | None, next_part: str
) -> None:
    history = _recorded_history("login_card")
    chat_id = history["workflow_copilot_chat_id"]
    server.set_history(chat_id, history)
    server.browser_session = browser_session
    server.card_routes["credential-response"] = httpx.Response(
        200, json={"result": "accepted", "expires_at": NOW.isoformat()}
    )

    result = await _call_recorded(
        ember.skyvern_ember_decide,
        history,
        ember_chat_id=chat_id,
        item_id=_login_item_id(history),
        decision="sign_in_myself",
    )

    assert server.bodies("/credential-response")[0]["action"] == "signing_in"
    assert result["data"]["outcome"]["live_browser_url"] == live_browser_url
    assert result["data"]["outcome"]["expires_at"] == NOW.isoformat()
    assert next_part in result["data"]["next"] and "decision='signed_in'" in result["data"]["next"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "decision", "chosen", "sent"),
    [
        (
            "account_group",
            "approve",
            "cred_b",
            {"account_group_decision": {"approved": True, "credential_ids": ["cred_b"]}},
        ),
        ("input_group", "approve", "cred_b", {"account_group_decision": {"approved": True, "item_keys": ["cred_b"]}}),
        ("account_group", "decline", None, {"account_group_decision": {"approved": False, "credential_ids": []}}),
        ("input_group", "decline", None, {"account_group_decision": {"approved": False, "credential_ids": []}}),
        ("credential_deletion", "approve", "cred_2", {"credential_ids": ["cred_2"]}),
    ],
    ids=["account_group", "input_group", "account_group_decline", "input_group_decline", "credential_deletion"],
)
async def test_group_decision_sends_only_what_the_user_chose(
    server: FakeEmber, kind: str, decision: str, chosen: str | None, sent: dict[str, Any]
) -> None:
    deleted = [{"credential_id": chosen, "outcome": "deleted"}]
    if kind == "credential_deletion":
        history = _deletion_history()
        item = history["question_interactions"][0]
        endpoint = "credential-deletion"
        review = item["credential_delete_review"] | {"outcomes": deleted}
        record = item | {"status": "resolved", "credential_delete_review": review}
    else:
        history = _recorded_history("run_approval")
        item = history["question_interactions"][0]
        endpoint = "question-response"
        item["account_group_review"] |= {
            "single_run": False,
            "row_group": kind == "input_group",
            "rows": [{"credential_id": f"cred_{name}", "label": name} for name in ("a", "b", "c")],
        }
        stored = {"credential_ids": [], **sent["account_group_decision"]}
        record = item | {
            "status": "resolved",
            "account_group_review": item["account_group_review"] | {"decision": stored},
        }
    chat_id = history["workflow_copilot_chat_id"]
    server.set_history(chat_id, history)
    server.card_routes[endpoint] = httpx.Response(200, json=record)

    result = await _call_recorded(
        ember.skyvern_ember_decide,
        history,
        ember_chat_id=chat_id,
        item_id=item["interaction_id"],
        decision=decision,
        credential_ids=[chosen] if chosen else None,
    )

    assert result["ok"], result
    assert server.bodies(f"/{endpoint}") == [
        {"ember_chat_id": chat_id, "interaction_id": item["interaction_id"], **sent}
    ]
    if kind == "credential_deletion":
        assert result["data"]["outcome"]["deletions"] == deleted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "sent", "expected_code", "approved"),
    [
        ("stored_decline", "approve", "ITEM_EXPIRED", False),
        ("stored_approval", "decline", "ITEM_EXPIRED", True),
        ("stored_approval", "approve", None, True),
        ("skipped", "approve", "ITEM_EXPIRED", False),
        ("deletion_approved", "decline", "ITEM_EXPIRED", True),
    ],
    ids=[
        "approve_after_stored_decline",
        "decline_after_stored_approval",
        "same_decision",
        "approve_after_skip",
        "deletion_decline_after_approval",
    ],
)
async def test_a_run_approval_answered_elsewhere_first_reports_the_stored_decision(
    server: FakeEmber, case: str, sent: str, expected_code: str | None, approved: bool
) -> None:
    deleted = [{"credential_id": "cred_2", "outcome": "deleted"}]
    if case == "deletion_approved":
        history = _deletion_history()
        item = history["question_interactions"][0]
        record = item | {
            "status": "resolved",
            "response": {"answers": [], "text": None, "skipped": False},
            "credential_delete_review": item["credential_delete_review"] | {"outcomes": deleted},
        }
    else:
        history = _recorded_history("run_approval")
        item = history["question_interactions"][0]
        record = item | {"status": "resolved"}
        if case == "skipped":
            record["response"] = {"answers": [], "text": None, "skipped": True}
        else:
            stored = {"approved": case == "stored_approval", "credential_ids": []}
            record["account_group_review"] = item["account_group_review"] | {"decision": stored}
    chat_id = history["workflow_copilot_chat_id"]
    server.set_history(chat_id, history)
    server.card_routes["question-response"] = httpx.Response(200, json=record)

    result = await _call_recorded(
        ember.skyvern_ember_decide, history, ember_chat_id=chat_id, item_id=item["interaction_id"], decision=sent
    )

    assert result["data"]["outcome"]["approved"] is approved
    if expected_code is None:
        assert result["ok"], result
    else:
        assert result["error"]["code"] == expected_code
        assert result["data"]["decision"] == ("approve" if approved else "decline")
    if case == "deletion_approved":
        assert result["data"]["outcome"]["deletions"] == deleted


def _single_run_with_a_long_input() -> dict[str, Any]:
    history = _recorded_history("run_approval")
    history["question_interactions"][0]["account_group_review"]["common_inputs"] = {"message": "x" * 9000}
    history["workflow_copilot_chat_id"] = CHAT
    return history


def _save_and_run_beside_a_cut_review() -> dict[str, Any]:
    history = _recorded_history("run_approval") | {"workflow_copilot_chat_id": CHAT}
    draft = _proposal_with_review_blocks(300)
    owner_turn_id = draft["proposed_workflow_metadata"]["owner_turn_id"]
    history["question_interactions"][0]["account_group_review"] |= {
        "single_run": True,
        "saves_draft": True,
        "proposal_owner_turn_id": owner_turn_id,
        "proposal_revision": draft["proposed_workflow_metadata"]["revision"],
    }
    owner = next(row for row in draft["chat_history"] if row["sender"] == "ai" and row.get("turn_id") == owner_turn_id)
    history |= {key: draft[key] for key in ("proposed_workflow", "proposed_workflow_metadata")}
    history["chat_history"] = [owner, *history["chat_history"]]
    return history


def _save_and_run_of_a_draft_still_being_built() -> dict[str, Any]:
    # The running turn owns the draft, so no finished row carries its review yet.
    history = _recorded_history("run_approval") | {"workflow_copilot_chat_id": CHAT}
    running = history["in_flight_turn"]["turn_id"]
    history["proposed_workflow_metadata"] |= {"owner_turn_id": running, "revision": 4}
    history["question_interactions"][0]["account_group_review"] |= {
        "single_run": True,
        "saves_draft": True,
        "proposal_owner_turn_id": running,
        "proposal_revision": 4,
    }
    return history


def _run_approval_with_120_rows() -> dict[str, Any]:
    history = _recorded_history("run_approval")
    rows = [{"credential_id": f"cred_{index}", "label": f"Account {index}"} for index in range(120)]
    history["question_interactions"][0]["account_group_review"] |= {"single_run": False, "rows": rows}
    history["workflow_copilot_chat_id"] = CHAT
    return history


def _login_card_now(card_now: str) -> Callable[[], dict[str, Any]]:
    # A late decision names the earlier card; the chat now holds a later pause or no card at all.
    if card_now == "a_later_pause":
        return lambda: _with_card(_recorded_history("login_card"), resume_token="a-later-pause-in-the-same-turn")
    return lambda: _recorded_history("login_card") | {"pending_credential_requests": []}


EARLIER_LOGIN_ITEM = _login_item_id(_recorded_history("login_card"))
PICK_SAVED_LOGIN = {"decision": "use_saved_login", "credential_ids": ["cred_portal"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build", "item_id", "kwargs", "code"),
    [
        (_deletion_history, "q_delete", {"decision": "approve"}, "INVALID_INPUT"),
        (_deletion_history, "q_delete", {"decision": "approve", "credential_ids": ["cred_9"]}, "INVALID_INPUT"),
        (_deletion_history, "q_delete", {"decision": "accept"}, "INVALID_INPUT"),
        (_deletion_history, "q_delete", {"decision": "decline", "credential_ids": ["cred_1"]}, "INVALID_INPUT"),
        (_deletion_history, "trigger:etr_596979605654677482", {"decision": "approve"}, "INVALID_INPUT"),
        (_deletion_history, "trigger:et_gmail", {"decision": "accept"}, "INVALID_INPUT"),
        (
            _run_approval_with_120_rows,
            "4f0b28615bb745dfa18df47613a70c38",
            {"decision": "approve", "credential_ids": [f"cred_{index}" for index in range(120)]},
            "INVALID_INPUT",
        ),
        (_single_run_with_a_long_input, "4f0b28615bb745dfa18df47613a70c38", {"decision": "approve"}, "INVALID_INPUT"),
        (
            _save_and_run_beside_a_cut_review,
            "4f0b28615bb745dfa18df47613a70c38",
            {"decision": "approve"},
            "INVALID_INPUT",
        ),
        (
            _save_and_run_of_a_draft_still_being_built,
            "4f0b28615bb745dfa18df47613a70c38",
            {"decision": "approve"},
            "INVALID_INPUT",
        ),
        (_login_card_now("a_later_pause"), EARLIER_LOGIN_ITEM, {"decision": "skip"}, "ITEM_EXPIRED"),
        (_login_card_now("a_later_pause"), EARLIER_LOGIN_ITEM, PICK_SAVED_LOGIN, "ITEM_EXPIRED"),
        (_login_card_now("none"), EARLIER_LOGIN_ITEM, {"decision": "skip"}, "ITEM_EXPIRED"),
        (_login_card_now("none"), EARLIER_LOGIN_ITEM, PICK_SAVED_LOGIN, "ITEM_EXPIRED"),
        (
            partial(_proposal_with_review_blocks, 300),
            _proposal_id(_recorded_history("finished_tested_proposal")),
            {"decision": "accept"},
            "INVALID_INPUT",
        ),
    ],
    ids=[
        "no_subset",
        "unknown_credential",
        "not_allowed",
        "ids_on_decline",
        "webhook_trigger",
        "gmail_trigger",
        "approval_too_large_to_show",
        "approval_with_a_clipped_input",
        "save_and_run_beside_a_cut_review",
        "save_and_run_of_a_draft_still_being_built",
        "late_skip_after_a_later_pause",
        "late_login_pick_after_a_later_pause",
        "late_skip_after_the_card_is_gone",
        "late_login_pick_after_the_card_is_gone",
        "accept_with_a_review_too_large_to_show",
    ],
)
async def test_decide_refusals_post_nothing(
    server: FakeEmber, build: Callable[[], dict[str, Any]], item_id: str, kwargs: dict[str, Any], code: str
) -> None:
    server.set_history(CHAT, build())

    result = await _call(ember.skyvern_ember_decide, ember_chat_id=CHAT, item_id=item_id, **kwargs)

    assert result["error"]["code"] == code
    assert _posts(server) == []


@pytest.mark.parametrize(
    ("status_code", "detail", "endpoint", "code"),
    [
        (409, "This login's sites changed since the card asked. Pick it again.", "credential-response", "ITEM_EXPIRED"),
        (410, "Credential pause has expired", "credential-response", "ITEM_EXPIRED"),
        (403, "Invalid credential resume token", "credential-response", "ITEM_EXPIRED"),
        (404, "No active credential pause for this turn", "credential-generate", "ITEM_EXPIRED"),
        (409, "Credential pause already resolved", "credential-generate", "ITEM_EXPIRED"),
        (404, "Unknown Copilot question", "question-response", "ITEM_EXPIRED"),
        (409, "Question is no longer pending", "credential-deletion", "ITEM_EXPIRED"),
        (409, "The question is no longer active", "question-response", "ITEM_EXPIRED"),
        (409, "The question's execution was interrupted", "credential-deletion", "ITEM_EXPIRED"),
        (409, "This deletion was already answered", "credential-deletion", "ITEM_EXPIRED"),
        (409, "Approve at least one account", "question-response", "INVALID_INPUT"),
        (409, "Unknown choice for this part", "question-response", "INVALID_INPUT"),
        (409, "Select at least one credential", "credential-deletion", "INVALID_INPUT"),
        (409, "A confirmation lists a credential the card did not", "credential-deletion", "INVALID_INPUT"),
        (404, "Unknown Copilot chat", "question-response", "CHAT_NOT_FOUND"),
        (404, "Chat not found", "apply-proposed-workflow", "CHAT_NOT_FOUND"),
        (404, "Credential cred_9 not found", "credential-response", "INVALID_INPUT"),
        (409, "This card did not ask about that login's site", "credential-response", "INVALID_INPUT"),
        (409, "Copilot proposal changed; reload required", "apply-proposed-workflow", "PROPOSAL_CHANGED"),
        (409, "Copilot proposal changed; reload required", "clear-proposed-workflow", "PROPOSAL_CHANGED"),
        (409, "Workflow changed after this proposal", "apply-proposed-workflow", "PROPOSAL_CHANGED"),
        (409, "Copilot proposal metadata is invalid; reload required", "apply-proposed-workflow", "PROPOSAL_CHANGED"),
        (409, "Copilot proposal is already being accepted", "apply-proposed-workflow", "PROPOSAL_CHANGED"),
        (409, "Ember proposal changed; reload required", "apply-proposed-workflow", "PROPOSAL_CHANGED"),
        (400, "No proposed workflow to apply", "apply-proposed-workflow", "NO_PROPOSAL"),
        (
            403,
            "Only the person who started this Copilot turn can answer it",
            "question-response",
            "TURN_CONTROL_DENIED",
        ),
        (403, "Credential deletion from Copilot is off", "credential-deletion", "PERMISSION_DENIED"),
        (403, "You can't use this login", "credential-response", "PERMISSION_DENIED"),
        (409, "Copilot is busy", "steer", "TURN_IN_PROGRESS"),
        (404, "Unknown Copilot chat", "cancel", "CHAT_NOT_FOUND"),
        (404, "Not Found", "cancel", "EMBER_UNAVAILABLE"),
        (503, "Cancel not supported in this environment", "cancel", "API_ERROR"),
        (403, "Missing permission", "cancel", "PERMISSION_DENIED"),
        (404, "workflow_run_id does not name a run of this workflow.", "chat-post", "RUN_NOT_FOUND"),
        (404, "Workflow not found", "chat-post", "WORKFLOW_NOT_FOUND"),
    ],
)
def test_card_route_errors_map_to_their_codes(
    status_code: int, detail: str, endpoint: _ember_http.Endpoint, code: str
) -> None:
    error = _ember_http.classify_error(httpx.Response(status_code, json={"detail": detail}), endpoint)

    assert error.code == code
    assert detail in error.message or code == "EMBER_UNAVAILABLE"
    assert isinstance(error, _ember_http.SitesChanged) == detail.startswith("This login's sites changed")


def _setup_answer(server: FakeEmber) -> tuple[Callable[..., Any], dict[str, Any], str]:
    history = _recorded_history("question")
    pending = next(item for item in history["question_interactions"] if item["status"] == "pending")
    server.set_history(history["workflow_copilot_chat_id"], history)
    server.card_routes["question-response"] = httpx.Response(200, json=pending | {"status": "resolved"})
    kwargs = {"ember_chat_id": history["workflow_copilot_chat_id"], "item_id": pending["interaction_id"], "text": "A"}
    return ember.skyvern_ember_answer, kwargs, "answer_recorded"


def _setup_decide_deletion(server: FakeEmber) -> tuple[Callable[..., Any], dict[str, Any], str]:
    history = _deletion_history()
    server.set_history(CHAT, history)
    server.card_routes["credential-deletion"] = httpx.Response(200, json=history["question_interactions"][0])
    kwargs = {"ember_chat_id": CHAT, "item_id": "q_delete", "decision": "approve", "credential_ids": ["cred_1"]}
    return ember.skyvern_ember_decide, kwargs, "outcome"


def _setup_decide_login(server: FakeEmber) -> tuple[Callable[..., Any], dict[str, Any], str]:
    history = _recorded_history("login_card")
    server.set_history(history["workflow_copilot_chat_id"], history)
    server.card_routes["credential-response"] = httpx.Response(200, json={"result": "accepted"})
    kwargs = {
        "ember_chat_id": history["workflow_copilot_chat_id"],
        "item_id": _login_item_id(history),
        "decision": "use_saved_login",
        "credential_ids": ["cred_portal"],
    }
    return ember.skyvern_ember_decide, kwargs, "outcome"


def _setup_decide_sites_changed(server: FakeEmber) -> tuple[Callable[..., Any], dict[str, Any], str]:
    history = _recorded_history("site_question")
    server.set_history(history["workflow_copilot_chat_id"], history)
    server.card_routes["credential-response"] = httpx.Response(409, json={"detail": "This login's sites changed."})
    kwargs = {
        "ember_chat_id": history["workflow_copilot_chat_id"],
        "item_id": _login_item_id(history),
        "decision": "use_saved_login",
        "credential_ids": [RECORDED_SITE_CREDENTIAL],
        "site_answer": "add",
    }
    return ember.skyvern_ember_decide, kwargs, "outcome"


def _setup_decide_accept(server: FakeEmber) -> tuple[Callable[..., Any], dict[str, Any], str]:
    history = _recorded_history("finished_tested_proposal")
    server.set_history(history["workflow_copilot_chat_id"], history)
    server.card_routes["apply-proposed-workflow"] = httpx.Response(200, json={"version": 3})
    kwargs = {
        "ember_chat_id": history["workflow_copilot_chat_id"],
        "item_id": _proposal_id(history),
        "decision": "accept",
    }
    return ember.skyvern_ember_decide, kwargs, "outcome"


def _setup_turn_off_auto_accept(server: FakeEmber) -> tuple[Callable[..., Any], dict[str, Any], str]:
    server.card_routes["disable-auto-accept"] = httpx.Response(204)
    return ember.skyvern_ember_decide, {"ember_chat_id": CHAT, "decision": "turn_off_auto_accept"}, "outcome"


def _setup_send_new_turn(server: FakeEmber) -> tuple[Callable[..., Any], dict[str, Any], str]:
    return ember.skyvern_ember_send, {"message": "Build it", "workflow_id": WPID}, "delivered_as"


def _setup_send_steer(server: FakeEmber) -> tuple[Callable[..., Any], dict[str, Any], str]:
    server.chats = [{"ember_chat_id": CHAT, "workflow_permanent_id": WPID, "in_flight": True}]
    server.set_history(CHAT, working_history())
    kwargs = {"message": "Use the second tab", "if_turn_running": "steer", "workflow_id": WPID}
    return ember.skyvern_ember_send, kwargs, "delivered_as"


def _setup_stop(server: FakeEmber) -> tuple[Callable[..., Any], dict[str, Any], str]:
    server.set_history(CHAT, working_history())
    return ember.skyvern_ember_stop, {"ember_chat_id": CHAT, "wait_seconds": 10}, "ember_chat_id"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup",
    [
        _setup_answer,
        _setup_decide_deletion,
        _setup_decide_login,
        _setup_decide_sites_changed,
        _setup_decide_accept,
        _setup_turn_off_auto_accept,
        _setup_send_new_turn,
        _setup_send_steer,
        _setup_stop,
    ],
)
@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500, json={"detail": "Internal Server Error"}),
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("read timed out"),
    ],
    ids=["history_500", "connect_error", "read_timeout"],
)
async def test_a_failed_read_after_a_write_still_reports_the_write(
    server: FakeEmber,
    setup: Callable[[FakeEmber], tuple[Callable[..., Any], dict[str, Any], str]],
    failure: httpx.Response | httpx.HTTPError,
) -> None:
    tool, kwargs, recorded_key = setup(server)
    server.history_after_post = failure

    result = await _call(tool, **kwargs)

    assert result["ok"], result
    assert result["data"]["ember_chat_id"] and recorded_key in result["data"]
    # Nothing was read after the write, so the state is not guessed.
    assert result["data"]["state"] == "unknown" and "skyvern_ember_check" in result["data"]["next"]
    assert len(_posts(server)) == 1
