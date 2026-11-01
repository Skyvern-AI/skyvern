"""Nace Drex Max decision caller: System One request shapes, answer decoding, HTTP error mapping,
factory routing.

No test reaches the network: the client is exercised through ``httpx.MockTransport``."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from litellm.utils import ModelResponse

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.api.llm.api_handler_factory import (
    LLMAPIHandlerFactory,
    LLMCaller,
    get_decision_llm_api_handler,
    with_explicit_llm_override,
)
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.exceptions import (
    BaseLLMError,
    InvalidLLMResponseFormat,
    LLMProviderError,
    LLMProviderErrorRetryableTask,
)
from skyvern.forge.sdk.api.llm.nace_decision_llm_caller import (
    NACE_DECISION_LLM_KEY,
    NACE_DECISION_PROMPT_NAMES,
    NACE_MAX_BODY_BYTES,
    NACE_MAX_IMAGES,
    NACE_USD_PER_INPUT_TOKEN,
    NaceDecisionClient,
    NaceDecisionPromptError,
    build_decision_request,
    call_nace_decision,
    extract_state_and_images,
    nace_decision_cost_usd,
    nace_model_supports_images,
    parse_candidate_elements,
)
from skyvern.forge.sdk.api.llm.utils import parse_api_response
from skyvern.schemas.llm import LLMConfig

PNG_DATA_URL = "data:image/png;base64,iVBORw0KGgo="


def _choice(choice: str, probabilities: dict[str, float]) -> dict[str, Any]:
    return {
        "type": "choice",
        "choice": choice,
        "confidence": probabilities[choice],
        "probabilities": probabilities,
    }


def _noul(probability: float) -> dict[str, Any]:
    return {"type": "noul", "noul": probability}


def _render_page_classify(extracted_text: str = "Welcome back. Sign in to continue.") -> str:
    return prompt_engine.load_prompt(
        template="page-classify",
        current_url="https://example.com/login",
        extracted_text=extracted_text,
        options={"LOGIN": "A login form is shown", "DASHBOARD": "The signed-in dashboard is shown"},
    )


def _render_auto_completion(
    is_search: bool,
    elements: str,
    new_elements_ids: list[str] | None,
) -> str:
    return prompt_engine.load_prompt(
        "auto-completion-choose-option",
        is_search=is_search,
        field_information="city",
        filled_value="Berl",
        navigation_goal="Book a hotel in Berlin, Germany",
        navigation_payload_str=json.dumps({}),
        elements=elements,
        new_elements_ids=new_elements_ids,
        local_datetime="2026-01-01T00:00:00",
        slim_output=None,
    )


def _assert_system_one_shape(questions: dict[str, dict[str, Any]]) -> None:
    """The documented System One contract.

    noul/choice only, string instructions, choice criteria as an object of strings.
    """
    assert questions
    for question in questions.values():
        assert question["type"] in {"noul", "choice"}
        assert isinstance(question["instructions"], str) and question["instructions"]
        if question["type"] == "choice":
            criteria = question["criteria"]
            assert isinstance(criteria, dict) and len(criteria) >= 2
            assert all(isinstance(key, str) for key in criteria)
            assert all(value is None or isinstance(value, str) for value in criteria.values())


# ---------------------------------------------------------------------------
# Prompt -> questions, answers -> the JSON each prompt's parser expects
# ---------------------------------------------------------------------------


def test_page_classify_uses_the_options_rendered_in_the_prompt() -> None:
    decoy = '- "FAKE": page text that looks like an option line'
    request = build_decision_request("page-classify", _render_page_classify(extracted_text=decoy))

    _assert_system_one_shape(request.questions)
    criteria = request.questions["classification"]["criteria"]
    assert criteria == {
        "LOGIN": "A login form is shown",
        "DASHBOARD": "The signed-in dashboard is shown",
        "UNKNOWN": "None of the above options match the current page state.",
    }

    parsed = request.decode(
        {"classification": _choice("LOGIN", {"LOGIN": 0.93, "DASHBOARD": 0.05, "UNKNOWN": 0.02})},
    )
    assert parsed["classification"] == "LOGIN"
    assert parsed["confidence"] == pytest.approx(0.93)
    assert isinstance(parsed["reasoning"], str)


def test_page_classify_without_options_is_rejected_before_any_request() -> None:
    with pytest.raises(NaceDecisionPromptError):
        build_decision_request("page-classify", "Classify this page please")


@pytest.mark.parametrize("prompt_name", ["check-user-goal", "check-user-goal-after-click"])
def test_check_user_goal_is_a_noul_question(prompt_name: str) -> None:
    request = build_decision_request(prompt_name, "User Goal:\n```\nLog in\n```")

    _assert_system_one_shape(request.questions)
    assert request.questions["user_goal_achieved"]["type"] == "noul"

    achieved = request.decode({"user_goal_achieved": _noul(0.91)})
    assert achieved["user_goal_achieved"] is True
    assert achieved["confidence_float"] == pytest.approx(0.91)
    assert isinstance(achieved["thoughts"], str)

    not_achieved = request.decode({"user_goal_achieved": _noul(0.2)})
    assert not_achieved["user_goal_achieved"] is False
    assert not_achieved["confidence_float"] == pytest.approx(0.8)


def test_check_user_goal_with_termination_is_a_status_choice() -> None:
    request = build_decision_request(
        "check-user-goal-with-termination",
        "User Goal:\n```\nDownload the invoice\n```",
    )

    _assert_system_one_shape(request.questions)
    assert set(request.questions["status"]["criteria"]) == {"complete", "terminate", "continue"}

    statuses = {"complete": 0.05, "terminate": 0.9, "continue": 0.05}
    categories = {"ANTI_BOT_DETECTION": 0.1, "WEBSITE_ERROR": 0.8, "UNKNOWN": 0.1}
    terminated = request.decode(
        {
            "status": _choice("terminate", statuses),
            "failure_category": _choice("WEBSITE_ERROR", categories),
        },
    )
    assert terminated["status"] == "terminate"
    assert terminated["failure_categories"] == [
        {
            "category": "WEBSITE_ERROR",
            "confidence_float": 0.8,
            "reasoning": terminated["failure_categories"][0]["reasoning"],
        }
    ]

    completed = request.decode(
        {
            "status": _choice("complete", {"complete": 0.97, "terminate": 0.01, "continue": 0.02}),
            "failure_category": _choice("UNKNOWN", categories),
        },
    )
    assert completed["status"] == "complete"
    assert completed["failure_categories"] == []


def test_decisive_criterion_validate_returns_one_action() -> None:
    request = build_decision_request(
        "decisive-criterion-validate",
        "Complete Criterion:\n```\nOrder placed\n```",
    )

    _assert_system_one_shape(request.questions)
    parsed = request.decode({"action": _choice("COMPLETE", {"COMPLETE": 0.88, "TERMINATE": 0.12})})
    assert parsed["actions"] == [
        {
            "reasoning": parsed["actions"][0]["reasoning"],
            "confidence_float": 0.88,
            "action_type": "COMPLETE",
        }
    ]
    assert isinstance(parsed["thoughts"], str) and isinstance(parsed["page_info"], str)


def test_infer_action_type_ranks_actions_by_probability() -> None:
    request = build_decision_request(
        "infer-action-type",
        "Action instruction\n```\nClick the blue Submit button\n```",
    )

    _assert_system_one_shape(request.questions)
    probabilities = {
        "CLICK": 0.7,
        "HOVER": 0.2,
        "INPUT_TEXT": 0.05,
        "UPLOAD_FILE": 0.03,
        "SELECT_OPTION": 0.01,
        "UNKNOWN_ACTION": 0.01,
    }
    parsed = request.decode({"action_type": _choice("CLICK", probabilities)})
    assert parsed["error"] is None
    actions = parsed["inferred_actions"]
    ranked = [(a["action_type"], a["confidence_float"]) for a in actions]
    assert ranked[0] == ("CLICK", 0.7)
    assert [confidence for _, confidence in ranked] == sorted((c for _, c in ranked), reverse=True)
    assert "UNKNOWN_ACTION" not in {name for name, _ in ranked}

    unknown = request.decode(
        {"action_type": _choice("UNKNOWN_ACTION", {"CLICK": 0.1, "UNKNOWN_ACTION": 0.9})},
    )
    assert unknown == {
        "thought": unknown["thought"],
        "inferred_actions": [],
        "error": "UNKNOWN_ACTION",
    }


def test_parse_candidate_elements_collects_ids_and_subtree_text() -> None:
    berlin = '<li id="2" role="option">Berlin, <b>Germany</b></li>'
    html = f'<ul id="1">{berlin}<li id="3">Berlin, CT</li><img id="4"></ul>'
    candidates = {candidate.element_id: candidate for candidate in parse_candidate_elements(html)}

    assert set(candidates) == {"1", "2", "3", "4"}
    assert candidates["2"].text == "Berlin, Germany"
    assert candidates["2"].describe() == '<li role="option"> Berlin, Germany'
    assert candidates["1"].text == "Berlin, Germany Berlin, CT"


def test_auto_completion_asks_about_the_emerging_elements_only() -> None:
    html = '<ul id="1"><li id="2">Berlin, Germany</li><li id="3">Berlin, CT</li></ul>'
    request = build_decision_request(
        "auto-completion-choose-option",
        _render_auto_completion(False, html, new_elements_ids=["2", "3"]),
    )

    _assert_system_one_shape(request.questions)
    assert set(request.questions) == {"auto_completion_attempt", "option", "relevance"}
    assert set(request.questions["option"]["criteria"]) == {"2", "3", "NONE"}

    parsed = request.decode(
        {
            "auto_completion_attempt": _noul(0.97),
            "option": _choice("2", {"2": 0.9, "3": 0.08, "NONE": 0.02}),
            "relevance": _noul(0.9),
        },
    )
    assert parsed["auto_completion_attempt"] is True
    assert parsed["id"] == "2"
    assert parsed["value"] == "Berlin, Germany"
    assert parsed["relevance_float"] == pytest.approx(0.9)
    assert parsed["confidence_float"] == pytest.approx(0.9)
    assert "direct_searching" not in parsed

    none = request.decode(
        {
            "auto_completion_attempt": _noul(0.3),
            "option": _choice("NONE", {"2": 0.1, "3": 0.1, "NONE": 0.8}),
        },
    )
    assert none["auto_completion_attempt"] is False
    assert none["id"] is None
    assert none["value"] == ""
    assert none["relevance_float"] == 0.0


def test_auto_completion_search_bar_adds_direct_searching() -> None:
    html = '<div id="7">Berlin Hauptbahnhof</div>'
    request = build_decision_request(
        "auto-completion-choose-option",
        _render_auto_completion(True, html, None),
    )

    assert set(request.questions) == {
        "auto_completion_attempt",
        "option",
        "relevance",
        "direct_searching",
    }
    parsed = request.decode(
        {
            "auto_completion_attempt": _noul(0.9),
            "option": _choice("7", {"7": 0.6, "NONE": 0.4}),
            "relevance": _noul(0.85),
            "direct_searching": _noul(0.75),
        },
    )
    assert parsed["direct_searching"] is True
    assert parsed["id"] == "7"


def test_auto_completion_relevance_is_not_the_choice_probability() -> None:
    html = '<ul id="1"><li id="2">Berlin</li><li id="3">Berlin</li></ul>'
    request = build_decision_request(
        "auto-completion-choose-option",
        _render_auto_completion(False, html, new_elements_ids=["2", "3"]),
    )

    # Two identical suggestions split the choice probability, yet either one fully satisfies the
    # goal.
    parsed = request.decode(
        {
            "auto_completion_attempt": _noul(0.99),
            "option": _choice("2", {"2": 0.5, "3": 0.5, "NONE": 0.0}),
            "relevance": _noul(0.96),
        },
    )

    assert parsed["id"] == "2"
    assert parsed["confidence_float"] == pytest.approx(0.5)
    assert parsed["relevance_float"] == pytest.approx(0.96)
    assert parsed["relevance_float"] >= 0.8  # the threshold choose_auto_completion_dropdown applies


def test_auto_completion_without_candidates_only_asks_about_the_attempt() -> None:
    request = build_decision_request(
        "auto-completion-choose-option",
        _render_auto_completion(False, "<html></html>", None),
    )

    assert set(request.questions) == {"auto_completion_attempt"}
    parsed = request.decode({"auto_completion_attempt": _noul(0.1)})
    assert parsed["id"] is None and parsed["auto_completion_attempt"] is False


def test_unsupported_prompts_are_rejected() -> None:
    assert "extract-actions" not in NACE_DECISION_PROMPT_NAMES
    with pytest.raises(NaceDecisionPromptError):
        build_decision_request("extract-actions", "plan the next actions")
    with pytest.raises(NaceDecisionPromptError):
        build_decision_request(None, "")


def test_missing_answer_is_a_response_format_error() -> None:
    request = build_decision_request("check-user-goal", "goal")
    with pytest.raises(InvalidLLMResponseFormat):
        request.decode({"something_else": _noul(0.5)})


@pytest.mark.parametrize(
    "probability",
    [1.5, -0.1, float("nan"), float("inf"), True, False, "0.9", None],
)
def test_invalid_noul_probabilities_are_rejected(probability: Any) -> None:
    request = build_decision_request("check-user-goal", "goal")
    with pytest.raises(InvalidLLMResponseFormat):
        request.decode({"user_goal_achieved": {"type": "noul", "noul": probability}})


@pytest.mark.parametrize(
    "answer",
    [
        {"choice": "complete"},
        {"choice": "complete", "probabilities": {}},
        {"choice": "complete", "probabilities": {"complete": 1.5, "continue": 0.0}},
        {"choice": "complete", "probabilities": {"complete": float("nan"), "continue": 0.5}},
        {"choice": "complete", "probabilities": {"complete": True, "continue": 0.0}},
        {"choice": "complete", "probabilities": {"continue": 1.0}},
        {
            "choice": "complete",
            "confidence": 2,
            "probabilities": {"complete": 0.9, "continue": 0.1},
        },
    ],
)
def test_invalid_choice_answers_are_rejected(answer: dict[str, Any]) -> None:
    request = build_decision_request("check-user-goal-with-termination", "goal")
    with pytest.raises(InvalidLLMResponseFormat):
        request.decode({"status": {"type": "choice", **answer}})


# ---------------------------------------------------------------------------
# Messages -> state + images
# ---------------------------------------------------------------------------


def test_extract_state_and_images_flattens_text_and_caps_images() -> None:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": "the prompt"}]
    image_block = {"type": "image_url", "image_url": {"url": PNG_DATA_URL}}
    blocks += [image_block] * (NACE_MAX_IMAGES + 2)
    messages = [{"role": "system", "content": "system prompt"}, {"role": "user", "content": blocks}]

    state, images = extract_state_and_images(messages)

    assert state == "system prompt\n\nthe prompt"
    assert images == [PNG_DATA_URL] * NACE_MAX_IMAGES


# ---------------------------------------------------------------------------
# HTTP client (mocked transport) and the ModelResponse handed back to LLMCaller
# ---------------------------------------------------------------------------


def _client(handler: Any) -> NaceDecisionClient:
    return NaceDecisionClient(api_key="test-key", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_client_sends_the_system_one_body_and_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.analytics.get_oss_version", lambda: "9.8.7+test")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        seen["client"] = request.headers["X-Nace-Client"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"model": "drex-max", "answers": {}, "usage": {"input_tokens": 1}},
        )

    questions = {"done": {"type": "noul", "instructions": "done?"}}
    payload = await _client(handler).decide(
        state="state",
        questions=questions,
        images=[PNG_DATA_URL],
        timeout=5,
    )

    assert payload["answers"] == {}
    assert seen["url"] == "https://console.nace.ai/v1/systemone"
    assert seen["authorization"] == "Bearer test-key"
    assert seen["client"] == "skyvern/9.8.7+test"  # the pip version, never "development"
    assert re.fullmatch(r"skyvern/[A-Za-z0-9 ._@+-]+", seen["client"]) and len(seen["client"]) <= 64
    assert seen["body"] == {
        "model": "drex-max",
        "state": "state",
        "questions": questions,
        "images": [PNG_DATA_URL],
    }


@pytest.mark.asyncio
async def test_client_omits_images_when_there_are_none() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"answers": {}})

    await _client(handler).decide(state="s", questions={"q": {"type": "noul", "instructions": "i"}})
    assert "images" not in seen["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, LLMProviderError),
        (403, LLMProviderError),
        (422, LLMProviderError),
        (429, LLMProviderErrorRetryableTask),
        (500, LLMProviderErrorRetryableTask),
        (529, LLMProviderErrorRetryableTask),
    ],
)
async def test_client_maps_http_errors(status_code: int, expected: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={
                "error": {
                    "type": "invalid_request_error",
                    "message": "bad request",
                    "issues": [{"path": "questions.q.criteria", "message": "must be an object"}],
                },
                "request_id": "req_1",
            },
        )

    with pytest.raises(expected) as exc_info:
        await _client(handler).decide(
            state="s",
            questions={"q": {"type": "noul", "instructions": "i"}},
        )
    # Retryable is a subclass of LLMProviderError; a deterministic 4xx must not be classified as
    # retryable.
    is_retryable = isinstance(exc_info.value, LLMProviderErrorRetryableTask)
    assert is_retryable == (expected is LLMProviderErrorRetryableTask)
    assert "bad request" in str(exc_info.value)
    if status_code == 422:
        assert "questions.q.criteria: must be an object" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "retryable"),
    [
        (503, {"error": "overloaded"}, True),
        (503, [], True),
        (503, "overloaded", True),
        (500, {"error": ["a"], "issues": 3}, True),
        (429, {"error": {"message": None, "issues": "bad"}}, True),
        (422, {"error": {"message": "bad", "issues": [None, 3, {"path": "q"}]}}, False),
        (401, None, False),
    ],
)
async def test_client_error_message_tolerates_unexpected_json_shapes(
    status_code: int,
    body: Any,
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status_code, content=b"not json")
        return httpx.Response(status_code, json=body)

    with pytest.raises(LLMProviderError) as exc_info:
        await _client(handler).decide(
            state="s",
            questions={"q": {"type": "noul", "instructions": "i"}},
        )

    assert isinstance(exc_info.value, LLMProviderErrorRetryableTask) is retryable
    assert f"HTTP {status_code}" in str(exc_info.value)


@pytest.mark.asyncio
async def test_client_waits_at_least_sixty_seconds() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json={"answers": {}})

    client = _client(handler)
    await client.decide(
        state="s",
        questions={"q": {"type": "noul", "instructions": "i"}},
        timeout=10,
    )
    assert seen["timeout"]["read"] == 60
    await client.decide(
        state="s",
        questions={"q": {"type": "noul", "instructions": "i"}},
        timeout=120,
    )
    assert seen["timeout"]["read"] == 120


@pytest.mark.asyncio
async def test_client_transport_failure_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMProviderErrorRetryableTask):
        await _client(handler).decide(
            state="s",
            questions={"q": {"type": "noul", "instructions": "i"}},
        )


@pytest.mark.asyncio
async def test_call_nace_decision_returns_a_chat_shaped_model_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["images"] == [PNG_DATA_URL]
        assert set(body["questions"]) == {"classification"}
        return httpx.Response(
            200,
            json={
                "model": "drex-max",
                "answers": {
                    "classification": _choice(
                        "LOGIN",
                        {"LOGIN": 0.93, "DASHBOARD": 0.05, "UNKNOWN": 0.02},
                    ),
                },
                "usage": {"input_tokens": 307, "output_tokens": 311},
                "evaluation_time_ms": 180.8,
                "request_id": "req_abc",
            },
        )

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _render_page_classify()},
                {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
            ],
        },
    ]
    response = await call_nace_decision(
        _client(handler),
        messages,
        prompt_name="page-classify",
        timeout=5,
    )

    assert isinstance(response, ModelResponse)
    assert response.id == "req_abc"
    assert response.model == "drex-max"
    assert response.usage.prompt_tokens == 307
    assert response.usage.completion_tokens == 311
    parsed = parse_api_response(
        response,
        add_assistant_prefix=False,
        force_dict=True,
        prompt_name="page-classify",
    )
    assert parsed["classification"] == "LOGIN"
    assert parsed["confidence"] == pytest.approx(0.93)


@pytest.mark.asyncio
async def test_call_nace_decision_rejects_unsupported_prompts_without_a_request() -> None:
    client = NaceDecisionClient(api_key="test-key")
    client.decide = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(NaceDecisionPromptError):
        await call_nace_decision(
            client,
            [{"role": "user", "content": "plan"}],
            prompt_name="extract-actions",
        )
    client.decide.assert_not_awaited()


@pytest.mark.asyncio
async def test_call_nace_decision_without_answers_is_a_response_format_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "drex-max"})

    with pytest.raises(InvalidLLMResponseFormat):
        await call_nace_decision(
            _client(handler),
            [{"role": "user", "content": "goal"}],
            prompt_name="check-user-goal",
        )


# ---------------------------------------------------------------------------
# Factory routing: the NACE key never reaches litellm
# ---------------------------------------------------------------------------


@pytest.fixture
def nace_registered(monkeypatch: pytest.MonkeyPatch) -> Any:
    config = LLMConfig(
        "drex-max",
        [],
        supports_vision=True,
        add_assistant_prefix=False,
        temperature=None,
    )
    monkeypatch.setattr(LLMConfigRegistry, "validate_config", staticmethod(lambda key, cfg: None))
    LLMConfigRegistry._configs.pop(NACE_DECISION_LLM_KEY, None)
    LLMConfigRegistry.register_config(NACE_DECISION_LLM_KEY, config)
    LLMAPIHandlerFactory._handler_cache.pop(NACE_DECISION_LLM_KEY, None)
    yield config
    LLMConfigRegistry.deregister_config(NACE_DECISION_LLM_KEY)
    LLMAPIHandlerFactory._handler_cache.pop(NACE_DECISION_LLM_KEY, None)


def test_get_llm_api_handler_routes_the_nace_key_through_llm_caller(nace_registered: Any) -> None:
    handler = LLMAPIHandlerFactory.get_llm_api_handler(NACE_DECISION_LLM_KEY)

    caller = getattr(handler, "__self__", None)
    assert isinstance(caller, LLMCaller)
    assert caller.llm_key == NACE_DECISION_LLM_KEY


@pytest.mark.asyncio
async def test_dispatch_routes_the_nace_key_to_the_decision_call(
    nace_registered: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller = LLMCaller(NACE_DECISION_LLM_KEY)
    decision_call = AsyncMock(return_value=ModelResponse())
    monkeypatch.setattr(caller, "_call_nace_decision", decision_call)
    acompletion = AsyncMock()
    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", acompletion)

    messages = [{"role": "user", "content": "state"}]
    await caller._dispatch_llm_call(messages=messages, prompt_name="page-classify", timeout=12)

    decision_call.assert_awaited_once_with(messages, 12, prompt_name="page-classify")
    acompletion.assert_not_awaited()
    assert caller.supports_tool_choice() is False


@pytest.mark.asyncio
async def test_decision_call_requires_the_client(
    nace_registered: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_handler_factory, "app", SimpleNamespace(NACE_CLIENT=None))
    caller = LLMCaller(NACE_DECISION_LLM_KEY)

    with pytest.raises(ValueError, match="ENABLE_NACE"):
        await caller._call_nace_decision(
            [{"role": "user", "content": "goal"}],
            prompt_name="check-user-goal",
        )


@pytest.mark.asyncio
async def test_call_stats_price_input_tokens_only(nace_registered: Any) -> None:
    caller = LLMCaller(NACE_DECISION_LLM_KEY)
    response = ModelResponse(
        model="drex-max",
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "{}"},
            },
        ],
        usage={"prompt_tokens": 1_000_000, "completion_tokens": 300, "total_tokens": 1_000_300},
    )

    stats = await caller.get_call_stats(response)

    assert stats.llm_cost_available is True
    assert stats.llm_cost == pytest.approx(1_000_000 * NACE_USD_PER_INPUT_TOKEN)
    assert stats.llm_cost == pytest.approx(0.03)
    assert stats.input_tokens == 1_000_000
    assert stats.output_tokens == 300


def test_get_decision_llm_api_handler_falls_back_unless_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = object()
    decision = object()

    monkeypatch.setattr(api_handler_factory.settings, "ENABLE_NACE", False)
    monkeypatch.setattr(
        api_handler_factory,
        "app",
        SimpleNamespace(DECISION_LLM_API_HANDLER=decision),
    )
    assert get_decision_llm_api_handler(default) is default  # type: ignore[arg-type]

    monkeypatch.setattr(api_handler_factory.settings, "ENABLE_NACE", True)
    assert get_decision_llm_api_handler(default) is decision  # type: ignore[arg-type]

    monkeypatch.setattr(api_handler_factory, "app", SimpleNamespace(DECISION_LLM_API_HANDLER=None))
    assert get_decision_llm_api_handler(default) is default  # type: ignore[arg-type]

    monkeypatch.setattr(api_handler_factory, "app", SimpleNamespace())
    assert get_decision_llm_api_handler(default) is default  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Through the real handler path (LLMCaller.call)
# ---------------------------------------------------------------------------


def _handler_caller(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    model: str = "drex-max",
) -> tuple[LLMCaller, dict[str, Any]]:
    """An LLMCaller for the Nace key whose System One calls go to ``handler``.

    ``handler`` is an ``httpx.MockTransport`` function.
    """
    seen: dict[str, Any] = {}

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return handler(request)

    client = NaceDecisionClient(
        api_key="test-key",
        model=model,
        transport=httpx.MockTransport(recording_handler),
    )
    monkeypatch.setattr(api_handler_factory, "app", SimpleNamespace(NACE_CLIENT=client))
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)
    return LLMCaller(NACE_DECISION_LLM_KEY), seen


@pytest.mark.asyncio
async def test_a_reply_without_answers_stays_a_response_format_error_through_the_handler(
    nace_registered: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller, _ = _handler_caller(
        monkeypatch,
        lambda request: httpx.Response(200, json={"model": "drex-max"}),
    )

    with pytest.raises(InvalidLLMResponseFormat) as exc_info:
        await caller.call(prompt="Did the user log in?", prompt_name="check-user-goal")

    assert isinstance(exc_info.value, BaseLLMError)
    assert not isinstance(exc_info.value, LLMProviderError)
    assert exc_info.value.llm_key == NACE_DECISION_LLM_KEY
    assert exc_info.value.prompt_name == "check-user-goal"


@pytest.mark.asyncio
async def test_an_out_of_range_probability_is_rejected_through_the_handler(
    nace_registered: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = {"model": "drex-max", "answers": {"user_goal_achieved": {"type": "noul", "noul": 1.5}}}
    caller, _ = _handler_caller(monkeypatch, lambda request: httpx.Response(200, json=reply))

    with pytest.raises(InvalidLLMResponseFormat):
        await caller.call(prompt="Did the user log in?", prompt_name="check-user-goal")


@pytest.mark.asyncio
async def test_a_503_with_an_unexpected_error_envelope_stays_retryable_through_the_handler(
    nace_registered: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller, _ = _handler_caller(
        monkeypatch,
        lambda request: httpx.Response(503, json={"error": "overloaded"}),
    )

    with pytest.raises(LLMProviderErrorRetryableTask):
        await caller.call(prompt="Did the user log in?", prompt_name="check-user-goal")


@pytest.mark.asyncio
async def test_the_workflow_system_prompt_reaches_the_request_state(
    nace_registered: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = SimpleNamespace(
        workflow_system_prompt="Never accept a guest checkout as a completed order.",
    )
    reply = {
        "model": "drex-max",
        "answers": {"user_goal_achieved": _noul(0.9)},
        "usage": {"input_tokens": 10},
    }
    caller, seen = _handler_caller(monkeypatch, lambda request: httpx.Response(200, json=reply))

    parsed = await caller.call(
        prompt="User Goal:\nPlace an order",
        prompt_name="check-user-goal",
        system_prompt=task.workflow_system_prompt,
    )

    state = seen["body"]["state"]
    assert task.workflow_system_prompt in state
    assert state.index(task.workflow_system_prompt) < state.index("User Goal:")
    assert parsed["user_goal_achieved"] is True


@pytest.mark.asyncio
async def test_without_a_system_prompt_the_state_is_the_prompt(
    nace_registered: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = {"model": "drex-max", "answers": {"user_goal_achieved": _noul(0.9)}}
    caller, seen = _handler_caller(monkeypatch, lambda request: httpx.Response(200, json=reply))

    await caller.call(prompt="User Goal:\nPlace an order", prompt_name="check-user-goal")

    assert seen["body"]["state"] == "User Goal:\nPlace an order"


# ---------------------------------------------------------------------------
# Capabilities and pricing follow the selected or served model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "images", "usd_per_million"),
    [
        ("drex-max", True, 0.03),
        ("drex-max-v1.1", True, 0.03),
        ("drex-v2.0", False, 0.06),
        ("drex-latest", False, 0.06),
    ],
)
def test_capabilities_and_price_by_model(model: str, images: bool, usd_per_million: float) -> None:
    assert nace_model_supports_images(model) is images
    assert nace_decision_cost_usd(1_000_000, model) == pytest.approx(usd_per_million)


def test_an_unpriced_model_has_no_cost() -> None:
    assert nace_decision_cost_usd(1_000, "drex-unknown") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("served", "usd_per_million"), [("drex-max", 0.03), ("drex-v2.0", 0.06)])
async def test_call_stats_price_by_the_served_model(
    nace_registered: Any,
    served: str,
    usd_per_million: float,
) -> None:
    response = ModelResponse(
        model=served,
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "{}"},
            },
        ],
        usage={"prompt_tokens": 1_000_000, "completion_tokens": 5, "total_tokens": 1_000_005},
    )

    stats = await LLMCaller(NACE_DECISION_LLM_KEY).get_call_stats(response)

    assert stats.llm_cost_available is True
    assert stats.llm_cost == pytest.approx(usd_per_million)


@pytest.mark.asyncio
async def test_call_stats_without_a_known_price_report_the_cost_as_unavailable(
    nace_registered: Any,
) -> None:
    response = ModelResponse(
        model="drex-unknown",
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "{}"},
            },
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )

    stats = await LLMCaller(NACE_DECISION_LLM_KEY).get_call_stats(response)

    assert stats.llm_cost_available is False


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["drex-v2.0", "drex-latest"])
async def test_text_only_models_are_never_sent_images(model: str) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"model": "drex-v2.0", "answers": {"user_goal_achieved": _noul(0.9)}},
        )

    client = NaceDecisionClient(api_key="k", model=model, transport=httpx.MockTransport(handler))
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "goal"},
                {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
            ],
        },
    ]

    response = await call_nace_decision(client, messages, prompt_name="check-user-goal")

    assert seen["body"]["model"] == model
    assert "images" not in seen["body"]
    assert response.model == "drex-v2.0"  # the served id, not the alias


_REGISTRY_PROBE = """
import json
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
issue = LLMConfigRegistry.get_config_issue("NACE_DREX_MAX")
registered = LLMConfigRegistry.is_registered("NACE_DREX_MAX")
vision = LLMConfigRegistry.get_config("NACE_DREX_MAX").supports_vision if registered else None
found = {"issue": issue is not None, "registered": registered, "vision": vision}
print("PROBE" + json.dumps(found))
"""


def _probe_registry(**env: str) -> dict[str, Any]:
    import os

    inherited = {k: v for k, v in os.environ.items() if not k.startswith(("NACE_", "ENABLE_NACE"))}
    child_env = dict(inherited)
    child_env.update(env)
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _REGISTRY_PROBE],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    line = next(line for line in completed.stdout.splitlines() if "PROBE" in line)
    return json.loads(line[line.index("PROBE") + len("PROBE") :])


def test_missing_key_outside_local_records_an_issue_instead_of_failing_import() -> None:
    probe = _probe_registry(ENABLE_NACE="true", ENV="production")

    assert probe == {"issue": True, "registered": False, "vision": None}


@pytest.mark.parametrize(
    ("model", "vision"),
    [("drex-max", True), ("drex-max-v1.1", True), ("drex-v2.0", False), ("drex-latest", False)],
)
def test_registered_config_supports_vision_only_for_image_models(model: str, vision: bool) -> None:
    probe = _probe_registry(
        ENABLE_NACE="true",
        ENV="production",
        NACE_API_KEY="k",
        NACE_MODEL=model,
    )

    assert probe == {"issue": False, "registered": True, "vision": vision}


# ---------------------------------------------------------------------------
# An explicit per-run llm_key outranks the deployment-wide opt-in
# ---------------------------------------------------------------------------


@pytest.fixture
def nace_enabled(monkeypatch: pytest.MonkeyPatch) -> Any:
    """ENABLE_NACE with a decision handler, a platform default, and a registered per-run model."""
    handlers = SimpleNamespace(
        decision=AsyncMock(name="nace"),
        platform=AsyncMock(name="platform"),
        explicit=AsyncMock(name="explicit"),
    )
    app_stub = SimpleNamespace(
        DECISION_LLM_API_HANDLER=handlers.decision,
        AUTO_COMPLETION_LLM_API_HANDLER=handlers.platform,
        CHECK_USER_GOAL_LLM_API_HANDLER=handlers.platform,
        SECONDARY_LLM_API_HANDLER=handlers.platform,
        LLM_API_HANDLER=handlers.platform,
    )
    monkeypatch.setattr(api_handler_factory.settings, "ENABLE_NACE", True)
    monkeypatch.setattr(api_handler_factory, "app", app_stub)
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(
        LLMAPIHandlerFactory,
        "get_llm_api_handler",
        staticmethod(
            lambda key: handlers.explicit if key == "EXPLICIT_RUN_MODEL" else handlers.decision,
        ),
    )
    return SimpleNamespace(handlers=handlers, app=app_stub)


def test_with_explicit_llm_override_wins_only_when_set_and_enabled(
    nace_enabled: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = nace_enabled.handlers.decision
    explicit = nace_enabled.handlers.explicit
    assert with_explicit_llm_override("EXPLICIT_RUN_MODEL", decision) is explicit
    assert with_explicit_llm_override(None, decision) is decision
    assert with_explicit_llm_override("", decision) is decision

    monkeypatch.setattr(api_handler_factory.settings, "ENABLE_NACE", False)
    assert with_explicit_llm_override("EXPLICIT_RUN_MODEL", decision) is decision


def test_auto_completion_handler_prefers_the_run_override_to_nace(
    nace_enabled: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.webeye.actions import handler as actions_handler

    monkeypatch.setattr(actions_handler, "app", nace_enabled.app)
    chosen = actions_handler._auto_completion_llm_handler(
        SimpleNamespace(llm_key="EXPLICIT_RUN_MODEL"),
    )
    assert chosen is nace_enabled.handlers.explicit

    # Both autocomplete calls use this handler; without an override the opt-in applies.
    chosen = actions_handler._auto_completion_llm_handler(SimpleNamespace(llm_key=None))
    assert chosen is nace_enabled.handlers.decision


@pytest.mark.asyncio
async def test_after_click_verification_prefers_the_run_override_to_nace(
    nace_enabled: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.webeye.actions import handler as actions_handler

    monkeypatch.setattr(actions_handler, "app", nace_enabled.app)

    async def keep_default(distinct_id: str, organization_id: str | None, default: Any) -> Any:
        return default

    monkeypatch.setattr(actions_handler, "resolve_check_user_goal_handler", keep_default)
    task = SimpleNamespace(llm_key="EXPLICIT_RUN_MODEL", organization_id="o_1")
    after_click = actions_handler._after_click_check_user_goal_handler
    assert await after_click(task, "wr_1") is nace_enabled.handlers.explicit

    task = SimpleNamespace(llm_key=None, organization_id="o_1")
    assert await after_click(task, "wr_1") is nace_enabled.handlers.decision


@pytest.mark.asyncio
async def test_page_classification_prefers_the_run_override_to_nace(
    nace_enabled: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.core.script_generations import real_skyvern_page_ai

    context = SimpleNamespace(
        organization_id="o_1",
        task_id="t_1",
        org_default_secondary_llm_key=None,
        org_default_llm_key=None,
    )
    tasks = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(llm_key="EXPLICIT_RUN_MODEL")),
    )
    page_ai_app = SimpleNamespace(**vars(nace_enabled.app), DATABASE=SimpleNamespace(tasks=tasks))
    monkeypatch.setattr(real_skyvern_page_ai, "app", page_ai_app)
    monkeypatch.setattr(real_skyvern_page_ai.skyvern_context, "current", lambda: context)
    monkeypatch.setattr(real_skyvern_page_ai.settings, "ENABLE_NACE", True)

    assert await real_skyvern_page_ai._page_classify_llm_handler() is nace_enabled.handlers.explicit

    tasks.get_task.return_value = SimpleNamespace(llm_key=None)
    assert await real_skyvern_page_ai._page_classify_llm_handler() is nace_enabled.handlers.decision


# ---------------------------------------------------------------------------
# Request body size, decorators, remaining call sites, inactive opt-in
# ---------------------------------------------------------------------------


def _noisy_png_data_url(red: int) -> str:
    import base64
    import io
    import os

    from PIL import Image

    pixels = bytearray(os.urandom(1920 * 1080 * 3))
    pixels[0::3] = bytes([red]) * (1920 * 1080)  # the red channel identifies the screenshot
    out = io.BytesIO()
    Image.frombytes("RGB", (1920, 1080), bytes(pixels)).save(out, format="PNG", compress_level=1)
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")


@pytest.mark.asyncio
async def test_oversized_screenshots_are_shrunk_to_fit_and_the_newest_is_kept() -> None:
    import base64
    import io

    from PIL import Image, ImageStat

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["size"] = len(request.content)
        seen["body"] = json.loads(request.content)
        answers = {"user_goal_achieved": _noul(0.9)}
        return httpx.Response(200, json={"model": "drex-max", "answers": answers})

    client = _client(handler)
    reds = [10, 90, 170, 250]
    blocks: list[dict[str, Any]] = [{"type": "text", "text": "goal"}]
    urls = [_noisy_png_data_url(red) for red in reds]
    blocks += [{"type": "image_url", "image_url": {"url": url}} for url in urls]

    messages = [{"role": "user", "content": blocks}]
    await call_nace_decision(client, messages, prompt_name="check-user-goal")

    assert seen["size"] <= NACE_MAX_BODY_BYTES
    kept = seen["body"]["images"]
    assert kept
    last = Image.open(io.BytesIO(base64.b64decode(kept[-1].split(",", 1)[1]))).convert("RGB")
    assert last.width * last.height <= 1_638_400
    mean_red = ImageStat.Stat(last).mean[0]  # JPEG re-encoding shifts single pixels, not the mean
    assert mean_red == pytest.approx(250, abs=25)  # the newest screenshot, not an older one


def test_small_screenshots_pass_through_untouched() -> None:
    from skyvern.forge.sdk.api.llm.nace_decision_llm_caller import fit_images_to_body_limit

    images = [PNG_DATA_URL, PNG_DATA_URL]
    assert fit_images_to_body_limit("state", {}, images) == images


def test_choose_auto_completion_dropdown_keeps_its_traced_wrapper() -> None:
    from skyvern.webeye.actions import handler as actions_handler

    assert hasattr(actions_handler.choose_auto_completion_dropdown, "__wrapped__")
    assert not hasattr(actions_handler._auto_completion_llm_handler, "__wrapped__")
    assert not hasattr(actions_handler._after_click_check_user_goal_handler, "__wrapped__")


def test_inactive_opt_in_leaves_the_override_wrapper_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = AsyncMock(name="platform")
    monkeypatch.setattr(api_handler_factory.settings, "ENABLE_NACE", True)
    monkeypatch.setattr(api_handler_factory, "app", SimpleNamespace(DECISION_LLM_API_HANDLER=None))
    monkeypatch.setattr(
        LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        staticmethod(lambda *a, **k: pytest.fail("override must not be consulted")),
    )

    assert with_explicit_llm_override("EXPLICIT_RUN_MODEL", handler) is handler
    assert get_decision_llm_api_handler(handler) is handler


@pytest.mark.asyncio
async def test_page_classification_makes_no_task_lookup_when_the_opt_in_is_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.core.script_generations import real_skyvern_page_ai

    context = SimpleNamespace(
        organization_id="o_1",
        task_id="t_1",
        org_default_secondary_llm_key=None,
        org_default_llm_key=None,
    )
    get_task = AsyncMock()
    platform = AsyncMock(name="platform")
    page_ai_app = SimpleNamespace(
        DECISION_LLM_API_HANDLER=None,
        SECONDARY_LLM_API_HANDLER=platform,
        DATABASE=SimpleNamespace(tasks=SimpleNamespace(get_task=get_task)),
    )
    monkeypatch.setattr(api_handler_factory.settings, "ENABLE_NACE", True)
    monkeypatch.setattr(api_handler_factory, "app", page_ai_app)
    monkeypatch.setattr(real_skyvern_page_ai, "app", page_ai_app)
    monkeypatch.setattr(real_skyvern_page_ai.skyvern_context, "current", lambda: context)

    assert await real_skyvern_page_ai._page_classify_llm_handler() is platform
    get_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_act_infer_action_type_prefers_the_run_override_to_nace(
    nace_enabled: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skyvern.core.script_generations import real_skyvern_page_ai
    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext

    context = SkyvernContext(organization_id="o_1", task_id="t_1", step_id="s_1")
    task = SimpleNamespace(llm_key="EXPLICIT_RUN_MODEL")
    step = SimpleNamespace()
    get_task = AsyncMock(return_value=task)
    tasks = SimpleNamespace(get_task=get_task, get_step=AsyncMock(return_value=step))
    page_ai_app = SimpleNamespace(
        **vars(nace_enabled.app),
        SINGLE_INPUT_AGENT_LLM_API_HANDLER=nace_enabled.handlers.platform,
        DATABASE=SimpleNamespace(tasks=tasks),
    )
    nace_enabled.handlers.explicit.return_value = {"inferred_actions": []}  # ai_act stops here
    monkeypatch.setattr(real_skyvern_page_ai, "app", page_ai_app)
    monkeypatch.setattr(real_skyvern_page_ai.skyvern_context, "ensure_context", lambda: context)
    monkeypatch.setattr(real_skyvern_page_ai.skyvern_context, "current", lambda: context)

    async def resolve(prompt_type: str, default: Any) -> Any:
        return default

    monkeypatch.setattr(real_skyvern_page_ai, "_resolve_assist_llm_handler", resolve)
    page_ai = real_skyvern_page_ai.RealSkyvernPageAi.__new__(real_skyvern_page_ai.RealSkyvernPageAi)
    monkeypatch.setattr(page_ai, "_with_step_notes", lambda prompt: prompt, raising=False)

    await page_ai.ai_act("click the blue button")

    nace_enabled.handlers.explicit.assert_awaited_once()
    nace_enabled.handlers.decision.assert_not_awaited()
