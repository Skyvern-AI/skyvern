import pytest

from skyvern.forge.agent import _LLM_STEP_EXCEPTIONS, _require_actions_payload
from skyvern.forge.sdk.api.llm.exceptions import LLMResponseMissingActionsError
from skyvern.forge.sdk.api.llm.utils import _coerce_response_to_dict


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


def test_two_reasoning_dicts_plus_action_list_keeps_first_dict():
    first = {"page_info": "p1"}
    second = {"thoughts": "t2"}
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
