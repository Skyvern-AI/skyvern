import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMResponseFormat
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models import web_search_block as search_module
from skyvern.forge.sdk.workflow.models.block import TextPromptBlock, _default_text_prompt_schema
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.web_search_block import WebSearchBlock, WebSearchError
from skyvern.schemas.workflows import BlockStatus, WebSearchBlockYAML

SCHEMA_ECHO = {
    "type": "object",
    "properties": {
        "llm_response": {
            "description": "No results found in the provided data route to the requested domain.",
            "type": "string",
        }
    },
    "required": ["llm_response"],
}
PROVIDER_PAGE = {
    "results": [
        {"title": "Unrelated result", "url": "https://unrelated.test/page", "highlights": ["Unrelated details."]}
    ]
}
NORMALIZED_RESULTS = [
    {
        "title": "Unrelated result",
        "link": "https://unrelated.test/page",
        "snippet": "Unrelated details.",
        "display_link": "unrelated.test",
        "position": 1,
    }
]


@pytest.mark.parametrize("field_name", ["no_results_error_code", "no_match_error_code"])
def test_search_outcome_error_codes_strip_whitespace(field_name: str) -> None:
    with pytest.raises(ValidationError, match="Outcome error codes must not be blank."):
        WebSearchBlockYAML(label="search", query="q", **{field_name: "   "})

    block = WebSearchBlockYAML(label="search", query="q", prompt="Filter", **{field_name: " CODE "})
    assert getattr(block, field_name) == "CODE"

    for code in ("CODE", "C" * 100):
        padded_code = " " * 60 + code + " " * 60
        block = WebSearchBlockYAML(label="search", query="q", prompt="Filter", **{field_name: padded_code})
        assert getattr(block, field_name) == code

    with pytest.raises(ValidationError, match="at most 100 characters"):
        WebSearchBlockYAML(label="search", query="q", prompt="Filter", **{field_name: " " + "C" * 101 + " "})


def test_search_no_match_error_code_requires_prompt() -> None:
    with pytest.raises(ValidationError, match="No Match Error Code requires a Prompt."):
        WebSearchBlockYAML(label="search", query="q", no_match_error_code="NO_MATCH")

    block = WebSearchBlockYAML(label="search", query="q", no_match_error_code="NO_MATCH", prompt="Filter")
    assert block.no_match_error_code == "NO_MATCH"


@pytest.fixture
def search_setup(monkeypatch: pytest.MonkeyPatch) -> tuple[WebSearchBlock, WorkflowRunContext, AsyncMock]:
    now = datetime.now(UTC)
    block = WebSearchBlock(
        label="search",
        query="requested documents",
        provider="exa",
        prompt="Return only matching results.",
        output_parameter=OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="search_output",
            output_parameter_id="output-test",
            workflow_id="workflow-test",
            created_at=now,
            modified_at=now,
        ),
    )
    context = WorkflowRunContext(
        workflow_title="test",
        workflow_id="workflow-test",
        workflow_permanent_id="wpid-test",
        workflow_run_id="workflow-run-test",
        aws_client=MagicMock(),
    )
    handler = AsyncMock()
    monkeypatch.setattr(WebSearchBlock, "get_workflow_run_context", staticmethod(lambda _: context))
    monkeypatch.setattr(WebSearchBlock, "_request", AsyncMock(return_value=PROVIDER_PAGE))
    monkeypatch.setattr(search_module.settings, "EXA_API_KEY", "test-provider-key")
    monkeypatch.setattr(TextPromptBlock, "_resolve_default_llm_handler", AsyncMock(return_value=handler))
    monkeypatch.setattr(
        block_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", lambda llm_key, *, default: default
    )
    monkeypatch.setattr(block_module.app.DATABASE.observer, "get_workflow_run_block", AsyncMock(return_value=None))
    return block, context, handler


@pytest.mark.asyncio
async def test_search_retries_schema_echo_with_feedback(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
) -> None:
    block, context, handler = search_setup
    handler.side_effect = [SCHEMA_ECHO, {"llm_response": "No results matched."}]

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert result.failure_reason is None
    output = result.output_parameter_value
    assert output == {
        "query": block.query,
        "provider": "exa",
        "results": NORMALIZED_RESULTS,
        "total_count": 1,
        "prompt_output": "No results matched.",
        "raw_response": {"pages": [PROVIDER_PAGE]},
    }
    assert context.values["search_output"] == output
    assert handler.await_count == TextPromptBlock.schema_validation_max_attempts
    first, second = (call.kwargs["prompt"] for call in handler.await_args_list)
    prompt_block = block._prompt_block(context)
    assert prompt_block is not None
    failure = prompt_block._validate_response_against_json_schema(SCHEMA_ECHO)
    assert failure is not None
    payload = json.dumps({"query": block.query, "results": NORMALIZED_RESULTS}, ensure_ascii=False)
    schema_fence = "```json\n" + json.dumps(prompt_block.json_schema, indent=2) + "\n```"
    assert block.prompt is not None
    for prompt in (first, second):
        assert prompt.startswith(block.prompt)
        assert prompt.count(payload) == 1
        assert prompt.count(schema_fence) == 1
        assert prompt.count("```json") == 1
    assert failure not in first
    assert second.index(payload) < second.index(failure) < second.index(schema_fence)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,failure_reason",
    [
        (SCHEMA_ECHO, "The Prompt response did not match the JSON output schema."),
        (InvalidLLMResponseFormat("invalid JSON"), "Web search succeeded, but Prompt processing failed."),
    ],
    ids=["schema-echo", "response-format"],
)
async def test_search_fails_after_prompt_attempts(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
    response: object,
    failure_reason: str,
) -> None:
    block, context, handler = search_setup
    handler.side_effect = [response] * TextPromptBlock.schema_validation_max_attempts

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.failure_reason == failure_reason
    assert handler.await_count == TextPromptBlock.schema_validation_max_attempts
    assert context.values["search_output"]["results"] == NORMALIZED_RESULTS
    assert context.values["search_output"]["total_count"] == 1
    assert context.values["search_output"]["prompt_output"] is None


@pytest.mark.asyncio
async def test_search_accepts_empty_array(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
) -> None:
    block, context, handler = search_setup
    block.json_schema = {"type": "array", "items": {"type": "string"}}
    handler.return_value = []

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert result.output_parameter_value["prompt_output"] == []
    assert context.values["search_output"]["prompt_output"] == []
    assert handler.await_count == 1


@pytest.mark.asyncio
async def test_search_rejects_invalid_schema_before_llm_call(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
) -> None:
    block, _, handler = search_setup
    block.json_schema = {"type": "invalid-type"}

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.failure_reason == "The Prompt JSON output schema is invalid."
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_page,num_results,expected_count",
    [
        (TimeoutError("Google search timed out after 30 seconds."), 11, 9),
        (WebSearchError("Google search failed (HTTP 429)."), 11, 9),
        *[
            (
                {
                    "search_metadata": {"status": "Success"},
                    "organic_results": [{"link": "https://unrelated.test/extra"}, {"title": "Missing URL"}],
                },
                num_results,
                expected_count,
            )
            for num_results, expected_count in [(11, 9), (10, 10)]
        ],
    ],
    ids=["timeout", "provider-error", "malformed-page", "malformed-after-cap"],
)
async def test_search_completes_with_validated_partial_results(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    second_page: Exception | dict[str, Any],
    num_results: int,
    expected_count: int,
) -> None:
    original, context, handler = search_setup
    block = original.model_copy(update={"provider": "google", "num_results": num_results})
    page = {
        "search_metadata": {"status": "Success"},
        "organic_results": [
            {"link": f"https://unrelated.test/{index}", "title": "Result", "snippet": "Details"} for index in range(9)
        ],
        "serpapi_pagination": {"next": "https://serpapi.com/search.json?start=10"},
    }
    monkeypatch.setattr(search_module.settings, "SERPAPI_API_KEY", "test-google-key")
    monkeypatch.setattr(WebSearchBlock, "_request", AsyncMock(side_effect=[page, second_page]))
    handler.return_value = {"llm_response": "Partial results processed."}

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    assert result.status == BlockStatus.completed
    assert result.success is True
    assert result.failure_reason is None
    output = result.output_parameter_value
    assert output["total_count"] == expected_count
    assert [item["position"] for item in output["results"]] == list(range(1, 10)) + (
        [11] if expected_count == 10 else []
    )
    assert output["prompt_output"] == "Partial results processed."
    assert output["raw_response"]["pages"] == ([page, second_page] if isinstance(second_page, dict) else [page])
    assert context.values["search_output"] == output
    handler.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "auto"])
async def test_search_first_page_timeout_preserves_fallback(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    original, _, handler = search_setup
    block = original.model_copy(update={"provider": provider})
    request = AsyncMock(side_effect=[TimeoutError("Google search timed out after 30 seconds."), PROVIDER_PAGE])
    monkeypatch.setattr(search_module.settings, "SERPAPI_API_KEY", "test-google-key")
    monkeypatch.setattr(WebSearchBlock, "_request", request)
    handler.return_value = {"llm_response": "Results processed."}

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    if provider == "google":
        assert result.status == BlockStatus.timed_out
        assert result.success is False
        handler.assert_not_awaited()
    else:
        assert result.status == BlockStatus.completed
        assert result.output_parameter_value["provider"] == "exa"
        assert request.await_args_list[1].args[0] == "exa"


@pytest.mark.asyncio
async def test_search_no_results_terminates_before_prompt(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block, context, handler = search_setup
    block.no_results_error_code = "NO_SEARCH_RESULTS"
    block.no_match_error_code = "NO_MATCHING_RESULT"
    monkeypatch.setattr(WebSearchBlock, "_request", AsyncMock(return_value={"results": []}))

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    assert result.status == BlockStatus.terminated
    assert result.success is False
    assert result.error_codes == ["NO_SEARCH_RESULTS"]
    assert result.failure_reason == "Web search returned no results."
    output = result.output_parameter_value
    assert output["status"] == "terminated"
    assert output["failure_reason"] == result.failure_reason
    assert output["errors"] == [
        {"error_code": "NO_SEARCH_RESULTS", "reasoning": result.failure_reason, "confidence_float": 1.0}
    ]
    assert output["results"] == []
    assert context.values["search_output"] == output
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [PROVIDER_PAGE, {"results": []}], ids=["results", "empty-results"])
@pytest.mark.parametrize(
    "answer",
    [
        {"match_found": False, "output": []},
        {"match_found": False, "output": None},
        {"match_found": True, "output": ["https://unrelated.test/page"]},
    ],
)
async def test_search_prompt_match_outcome(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    page: dict[str, Any],
    answer: dict[str, Any],
) -> None:
    block, context, handler = search_setup
    block.no_match_error_code = "NO_MATCHING_RESULT"
    block.json_schema = {"type": "array", "items": {"type": "string"}}
    monkeypatch.setattr(WebSearchBlock, "_request", AsyncMock(return_value=page))
    handler.return_value = answer

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    output = result.output_parameter_value
    assert output["prompt_output"] == answer["output"]
    assert context.values["search_output"] == output
    handler.assert_awaited()
    if answer["match_found"]:
        assert result.status == BlockStatus.completed
        assert result.success is True
        assert set(output) == {"query", "provider", "results", "total_count", "prompt_output", "raw_response"}
    else:
        assert result.status == BlockStatus.terminated
        assert result.success is False
        assert result.error_codes == ["NO_MATCHING_RESULT"]
        assert result.failure_reason == "No search result matched the Prompt."
        assert output["status"] == "terminated"
        assert output["errors"][0]["error_code"] == "NO_MATCHING_RESULT"


@pytest.mark.parametrize(
    "schema,good,bad",
    [
        (
            {"type": "array", "$defs": {"item": {"type": "string"}}, "items": {"$ref": "#/$defs/item"}},
            ["value"],
            [1],
        ),
        ({"type": "array", "items": {"type": "string"}, "minItems": 1}, ["value"], []),
        (_default_text_prompt_schema(), {"llm_response": "value"}, {"llm_response": 1}),
        (
            {"type": "object", "properties": {"child": {"$ref": "#"}}, "additionalProperties": False},
            {"child": {}},
            {"child": 1},
        ),
        (
            {
                "$id": "https://schema.test/output",
                "$defs": {"item": {"type": "string"}},
                "type": "array",
                "items": {"$ref": "#/$defs/item"},
            },
            ["value"],
            [1],
        ),
        (
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "definitions": {"item": {"type": "string"}},
                "type": "array",
                "items": {"$ref": "#/definitions/item"},
            },
            ["value"],
            [1],
        ),
    ],
    ids=["ref-defs", "min-items", "text-default", "recursive", "has-id", "definitions-draft7"],
)
def test_prompt_wrapper_preserves_schema_validation(schema: dict[str, Any], good: Any, bad: Any) -> None:
    original = deepcopy(schema)
    wrapper = search_module._wrap_prompt_schema(schema)
    Draft202012Validator.check_schema(wrapper)
    validator = Draft202012Validator(wrapper)
    assert schema == original
    for match_found, output, valid in [
        (True, good, True),
        (True, bad, False),
        (True, None, False),
        (False, None, True),
        (False, good, True),
        (False, bad, False),
    ]:
        assert validator.is_valid({"match_found": match_found, "output": output}) is valid
    assert not validator.is_valid({"match_found": True, "output": good, "extra": True})
    assert not validator.is_valid(wrapper)
