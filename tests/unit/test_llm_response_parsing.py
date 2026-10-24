import io
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from structlog.testing import capture_logs

from skyvern.forge.agent import (
    _LLM_STEP_EXCEPTIONS,
    _require_actions_payload,
    _terminate_action_from_llm_error_code,
)
from skyvern.forge.sdk.api.llm import utils as llm_utils
from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMResponseFormat, LLMResponseMissingActionsError
from skyvern.forge.sdk.api.llm.utils import _coerce_response_to_dict, parse_api_response
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.handler import handle_terminate_action
from tests.unit.helpers import make_organization, make_step, make_task


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"page_info": "Select country"}, ({"page_info": "Select country"}, False)),
        ([{"page_info": "First"}, {"page_info": "Second"}], ({"page_info": "First"}, False)),
        (["text", {"page_info": "First dict"}], ({"page_info": "First dict"}, False)),
        ([1, 2, 3], ({}, True)),
        ("not-a-dict", ({}, True)),
        ([], ({}, True)),
    ],
)
def test_coerce_response_to_dict_variants(response, expected):
    try:
        parsed = _coerce_response_to_dict(response)
        assert parsed == expected[0]
    except Exception:
        assert expected[1]


def test_coerce_response_warning_keeps_metadata_without_response_content():
    sentinel = "private-response-sentinel"
    response = [{"reasoning": sentinel}]

    with capture_logs() as events:
        assert _coerce_response_to_dict(response, "extract-actions") == response[0]

    event = next(event for event in events if event["event"] == "Parsed LLM response is not a dict")
    assert event["response_type"] == "list"
    assert event["response_length"] == 1
    assert event["prompt_name"] == "extract-actions"
    assert all(sentinel not in repr(event) for event in events)


@pytest.mark.parametrize("include_metadata", [True, False])
def test_parse_failure_log_keeps_metadata_without_response_content(monkeypatch, include_metadata):
    sentinel = "private-response-sentinel"
    content = f"not-json {sentinel}"
    response_metadata = {
        "id": "response-456",
        "model": "test-model",
        "usage": SimpleNamespace(prompt_tokens=8, completion_tokens=3, total_tokens=11),
    }
    response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))],
        **(response_metadata if include_metadata else {}),
    )

    output = io.StringIO()
    logger = structlog.wrap_logger(
        structlog.PrintLogger(file=output),
        processors=[structlog.processors.format_exc_info, structlog.processors.JSONRenderer()],
    )

    def fail_auto_fix(_content):
        raise ValueError("Unable to parse text", content)

    monkeypatch.setattr(llm_utils, "LOG", logger)
    monkeypatch.setattr(llm_utils, "_fix_and_parse_json_string", fail_auto_fix)
    with pytest.raises(InvalidLLMResponseFormat) as error:
        parse_api_response(response, prompt_name="extract-actions")
    assert sentinel not in str(error.value)
    assert error.value.__suppress_context__
    try:
        raise error.value
    except InvalidLLMResponseFormat:
        logger.exception("Caller logs malformed LLM response.")

    rendered_logs = output.getvalue()
    events = [json.loads(line) for line in rendered_logs.splitlines()]
    event = next(
        event
        for event in events
        if event["event"] == "Failed to parse LLM response. Will retry auto-fixing the response for unescaped quotes."
    )
    assert event["model"] == ("test-model" if include_metadata else None)
    assert event["prompt_name"] == "extract-actions"
    assert event["response_type"] == "SimpleNamespace"
    assert event["content_length"] == len(content)
    assert event["stop_reason"] == "stop"
    assert event["token_usage"] == (
        {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11} if include_metadata else None
    )
    assert event["response_id"] == ("response-456" if include_metadata else None)
    assert any(event["event"] == "Failed to auto-fix LLM response." for event in events)
    assert sentinel not in rendered_logs


def test_bare_action_list_wraps_into_actions_for_actions_prompt():
    actions = [
        {"reasoning": "Fill first name", "action_type": "INPUT_TEXT", "id": "a1", "text": "John"},
        {"reasoning": "Submit the form", "action_type": "CLICK", "id": "a2"},
    ]
    assert _coerce_response_to_dict(actions, "extract-actions") == {"actions": actions}


def test_bare_action_list_ignores_scalar_junk_elements():
    fill = {"reasoning": "Fill", "action_type": "INPUT_TEXT", "id": "a1", "text": "x"}
    click = {"reasoning": "Submit", "action_type": "CLICK", "id": "a2"}
    assert _coerce_response_to_dict([fill, "\n", click], "extract-actions") == {"actions": [fill, click]}


def test_bare_action_list_dedupes_consecutive_identical_actions():
    fill = {"reasoning": "Fill", "action_type": "INPUT_TEXT", "id": "a1", "text": "x"}
    click = {"reasoning": "Submit", "action_type": "CLICK", "id": "a2"}
    assert _coerce_response_to_dict([fill, click, click], "extract-actions") == {"actions": [fill, click]}
    assert _coerce_response_to_dict([click, fill, click], "extract-actions") == {"actions": [click, fill, click]}


def test_single_action_list_wraps_for_actions_prompt():
    # A lone COMPLETE/CLICK emitted as a bare one-element array must still be
    # recovered for actions-consuming prompts.
    action = {"action_type": "COMPLETE", "reasoning": "criterion met", "confidence_float": 1.0}
    assert _coerce_response_to_dict([action], "decisive-criterion-validate") == {"actions": [action]}


def test_bare_action_list_not_wrapped_for_non_actions_prompt():
    # custom-select returns a single object carrying action_type; its handler reads
    # json_response["action_type"] directly, so a split must keep first-dict, not wrap.
    first = {"reasoning": "California matches.", "action_type": "CLICK", "id": "opt-CA", "value": "California"}
    second = {"action_type": "CLICK", "id": "opt-CA"}
    assert _coerce_response_to_dict([first, second], "custom-select") == first


def test_single_element_list_unwraps_for_non_actions_prompt():
    verdict = {"action_type": "COMPLETE", "thoughts": "goal met", "user_goal_achieved": True}
    assert _coerce_response_to_dict([verdict]) == verdict
    assert _coerce_response_to_dict([verdict], "check-user-goal") == verdict


def test_reasoning_dict_with_nested_action_list_reattaches_actions():
    preamble = {"page_info": "Invoice displayed.", "thoughts": "Criterion met.", "account_number": "1234567890"}
    actions = [{"reasoning": "Criterion satisfied.", "confidence_float": 1.0, "action_type": "COMPLETE"}]
    assert _coerce_response_to_dict([preamble, actions]) == {**preamble, "actions": actions}


def test_reasoning_dict_and_action_list_order_agnostic():
    preamble = {"page_info": "Invoice displayed.", "thoughts": "Criterion met."}
    actions = [{"reasoning": "Criterion satisfied.", "action_type": "COMPLETE"}]
    assert _coerce_response_to_dict([actions, preamble]) == {**preamble, "actions": actions}


def test_reasoning_dict_with_nested_action_list_tolerates_scalar_junk():
    preamble = {"page_info": "Invoice displayed."}
    actions = [{"reasoning": "Done.", "action_type": "COMPLETE"}]
    assert _coerce_response_to_dict([preamble, "separator", actions]) == {**preamble, "actions": actions}


def test_dict_with_action_type_plus_nested_list_keeps_first_dict():
    complete = {"action_type": "COMPLETE", "confidence_float": 0.9}
    terminate = [{"action_type": "TERMINATE", "reasoning": "cannot proceed"}]
    assert _coerce_response_to_dict([complete, terminate]) == complete


def test_dict_with_own_actions_key_is_not_overridden_by_nested_list():
    own = {"page_info": "p", "actions": [{"action_type": "CLICK", "id": "a1"}]}
    stray = [{"action_type": "TERMINATE", "reasoning": "conflict"}]
    assert _coerce_response_to_dict([own, stray]) == own


def test_stray_fragments_between_preamble_and_action_list_still_reattach():
    # An unescaped quote inside `thoughts` ends that string early, so the rest of
    # the reasoning text is stranded as extra top-level fragments sitting between
    # the preamble and the action array.
    preamble = {"page_info": "p1", "thoughts": "OCR output is `{'document_category': 'BILLING INVOICE'"}
    dict_fragment = {"document_category": "BILLING INVOICE", "customer_number": "0206556463"}
    list_fragment = ["0206556463"]
    actions = [{"action_type": "COMPLETE", "reasoning": "done", "confidence_float": 1.0}]
    expected = {**preamble, "actions": actions}
    assert _coerce_response_to_dict([preamble, dict_fragment, actions], "decisive-criterion-validate") == expected
    assert _coerce_response_to_dict([preamble, list_fragment, actions], "decisive-criterion-validate") == expected


def test_later_dict_carrying_action_type_keeps_first_dict():
    first = {"page_info": "p1"}
    second = {"action_type": "TERMINATE", "reasoning": "competing decision"}
    actions = [{"action_type": "COMPLETE", "reasoning": "done"}]
    assert _coerce_response_to_dict([first, second, actions]) == first


def test_nested_list_without_action_type_is_not_attached():
    meta = {"title": "doc"}
    items = [{"name": "a"}, {"name": "b"}]
    assert _coerce_response_to_dict([meta, items]) == meta


def test_require_actions_payload_missing_key_raises_typed_error():
    with pytest.raises(LLMResponseMissingActionsError):
        _require_actions_payload({"page_info": "x", "thoughts": "y"})


@pytest.mark.parametrize("bad_actions", ["click the button", {"action_type": "CLICK"}, 42, None])
def test_require_actions_payload_non_list_raises_typed_error(bad_actions):
    with pytest.raises(LLMResponseMissingActionsError):
        _require_actions_payload({"page_info": "x", "actions": bad_actions})


def test_require_actions_payload_returns_present_value():
    assert _require_actions_payload({"actions": []}) == []
    actions = [{"action_type": "CLICK", "id": "a1"}]
    assert _require_actions_payload({"page_info": "p", "actions": actions}) == actions


def test_missing_actions_error_is_recognized_as_llm_step_failure():
    # summary_failure_reason_for_max_retries matches step_exception by exact class
    # name against _LLM_STEP_EXCEPTIONS; membership keeps steps failing at the
    # actions guard on the code-level failure-reason path instead of the
    # LLM-fabricated one.
    assert LLMResponseMissingActionsError.__name__ in _LLM_STEP_EXCEPTIONS


def _task_and_step(error_code_mapping):
    now = datetime.now()
    task = make_task(now, make_organization(now), error_code_mapping=error_code_mapping)
    return task, make_step(now, task, step_id="step-1", status=StepStatus.running, order=0, output=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mapping_key", "llm_codes"),
    [
        ("website_down", {"code": "WEBSITE_DOWN"}),
        # A single-token key is not recoverable from reasoning text, so it must survive extraction some other way.
        ("maintenance", {"code": "Maintenance"}),
        # An unmapped `code` must not hide a mapped `error_code`.
        ("website_down", {"code": 503, "error_code": "website_down"}),
    ],
)
async def test_empty_actions_with_mapped_error_code_terminates_with_that_code(mapping_key, llm_codes):
    # The model named a mapped code outside `actions` and planned nothing. The
    # synthesized TERMINATE must keep the code through handle_terminate_action even when the
    # surfacing extractor finds nothing, or the task retries to REACH_MAX_RETRIES instead.
    task, step = _task_and_step({mapping_key: "The site is down or under maintenance"})
    json_response = {
        "actions": [],
        # Model text can echo a secret typed earlier in the run; it must not reach persisted errors.
        "error": {**llm_codes, "reasoning": "Down after entering one-time code 918273."},
    }

    action = _terminate_action_from_llm_error_code(task, step, json_response)

    assert action is not None
    assert [error.error_code for error in action.errors] == [mapping_key]
    assert "918273" not in action.reasoning + "".join(error.reasoning for error in action.errors)
    scraped_page = MagicMock()
    scraped_page.refresh = AsyncMock(return_value=MagicMock(url="https://example.com", screenshots=[]))
    with (
        skyvern_context.scoped(SkyvernContext()),
        patch("skyvern.webeye.actions.handler.get_action_history", AsyncMock(return_value=[])),
        patch(
            "skyvern.webeye.actions.handler.get_org_aware_primary_llm_api_handler",
            return_value=AsyncMock(return_value={"errors": []}),
        ),
    ):
        await handle_terminate_action(action, MagicMock(), scraped_page, task, step)
    assert [error.error_code for error in action.errors] == [mapping_key]


@pytest.mark.parametrize(
    "json_response",
    [
        {"actions": []},
        {"actions": [], "error": {"code": "not_in_mapping", "reasoning": "x"}},
        {"actions": [], "error": "website_down"},
    ],
)
def test_empty_actions_without_mapped_error_code_is_not_terminated(json_response):
    task, step = _task_and_step({"website_down": "The site is down or under maintenance"})
    assert _terminate_action_from_llm_error_code(task, step, json_response) is None
