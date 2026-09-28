import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMResponseFormat
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models import web_search_block as search_module
from skyvern.forge.sdk.workflow.models.block import TextPromptBlock
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
@pytest.mark.parametrize("contents_fails", [False, True], ids=["uncached-page", "contents-failure"])
async def test_exa_search_keeps_pages_without_cached_content(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    contents_fails: bool,
) -> None:
    block, _, _ = search_setup
    block.prompt = None
    links = ["https://a.example.com/1", "https://b.example.com/2", "https://c.example.com/3"]
    payloads: dict[str, dict[str, Any]] = {}

    async def request(
        self: WebSearchBlock, provider: str, url: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        assert payload is not None
        payloads[url] = payload
        if url == "https://api.exa.ai/search":
            return {"results": [{"url": link} for link in links]}
        assert url == "https://api.exa.ai/contents"
        if contents_fails:
            raise WebSearchError("Exa search failed (HTTP 500).")
        return {
            "results": [
                {"url": links[0], "highlights": ["a1", "a2"]},
                {"url": links[2], "highlights": ["c1"]},
            ]
        }

    monkeypatch.setattr(WebSearchBlock, "_request", request)
    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    assert "contents" not in payloads["https://api.exa.ai/search"]
    assert payloads["https://api.exa.ai/contents"]["urls"] == links
    assert (
        payloads["https://api.exa.ai/contents"]["highlights"]["query"] == payloads["https://api.exa.ai/search"]["query"]
    )
    assert result.status == BlockStatus.completed
    results = result.output_parameter_value["results"]
    assert [item["link"] for item in results] == links
    assert [item["snippet"] for item in results] == (["", "", ""] if contents_fails else ["a1\na2", "", "c1"])


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
    first, second = (call.kwargs["prompt"] for call in handler.await_args_list)
    prompt_block = block._prompt_block(context, block.prompt or "", block.json_schema)
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
        (SCHEMA_ECHO, "The Prompt response did not match the Data Schema after 2 attempts:"),
        (InvalidLLMResponseFormat("invalid JSON"), "The Prompt response was not valid JSON after 2 attempts."),
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
    assert result.failure_reason.startswith(failure_reason)
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


@pytest.mark.asyncio
async def test_search_rejects_invalid_schema_before_llm_call(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
) -> None:
    block, _, handler = search_setup
    block.json_schema = {"type": "invalid-type"}

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.failure_reason.startswith("The Data Schema is not a valid JSON Schema:")
    assert result.output_parameter_value["failure_category"][0]["category"] == "DATA_EXTRACTION_FAILURE"
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


@pytest.mark.asyncio
async def test_google_site_filter_refetches_off_site_page_at_most_twice(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, _, _ = search_setup
    block = original.model_copy(update={"provider": "google", "query": "site:example.com documents", "prompt": None})
    off_site_page = {
        "search_metadata": {"status": "Success"},
        "organic_results": [{"link": "https://unrelated.test/a"}, {"link": "https://unrelated.test/b"}],
        "serpapi_pagination": {"next": "https://serpapi.com/search.json?start=10"},
    }
    request = AsyncMock(side_effect=[off_site_page] * 3)
    monkeypatch.setattr(search_module.settings, "SERPAPI_API_KEY", "test-google-key")
    monkeypatch.setattr(WebSearchBlock, "_request", request)

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    assert result.success is True
    assert result.status == BlockStatus.completed
    output = result.output_parameter_value
    assert output["results"] == []
    assert request.await_count == 3
    assert all(call.args[0] == "google" for call in request.await_args_list)
    parameters = [parse_qs(urlsplit(call.args[1]).query) for call in request.await_args_list]
    assert "no_cache" not in parameters[0]
    assert parameters[1] == parameters[2] == {**parameters[0], "no_cache": ["true"]}
    assert all(params["start"] == ["0"] for params in parameters)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "auto"])
async def test_search_first_page_timeout_preserves_fallback(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    original, _, handler = search_setup
    block = original.model_copy(update={"provider": provider})
    request = AsyncMock(
        side_effect=[TimeoutError("Google search timed out after 30 seconds."), PROVIDER_PAGE, PROVIDER_PAGE]
    )
    monkeypatch.setattr(search_module.settings, "SERPAPI_API_KEY", "test-google-key")
    monkeypatch.setattr(WebSearchBlock, "_request", request)
    handler.return_value = {"llm_response": "Results processed."}

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    if provider == "google":
        assert result.status == BlockStatus.timed_out
        assert result.success is False
    else:
        assert result.status == BlockStatus.completed
        assert result.output_parameter_value["provider"] == "exa"
        assert request.await_args_list[1].args[0] == "exa"


@pytest.mark.asyncio
async def test_search_no_results_detects_legacy_code_after_prompt(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block, context, handler = search_setup
    block.no_results_error_code = "NO_SEARCH_RESULTS"
    block.no_match_error_code = "NO_MATCHING_RESULT"
    handler.side_effect = [
        {"llm_response": "No results."},
        {
            "reasoning": "No results.",
            "errors": [{"error_code": "NO_SEARCH_RESULTS", "reasoning": "No results.", "confidence_float": 0.9}],
        },
    ]
    monkeypatch.setattr(WebSearchBlock, "_request", AsyncMock(return_value={"results": []}))

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    assert result.status == BlockStatus.terminated
    assert result.success is False
    assert result.error_codes == ["NO_SEARCH_RESULTS"]
    assert result.failure_reason == "No results."
    output = result.output_parameter_value
    assert output["status"] == "terminated"
    assert output["failure_reason"] == result.failure_reason
    assert output["errors"] == [
        {
            "error_code": "NO_SEARCH_RESULTS",
            "reasoning": result.failure_reason,
            "confidence_float": 0.9,
            "error_type": "USER_DEFINED_ERROR",
        }
    ]
    assert output["results"] == []
    assert context.values["search_output"] == output


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [PROVIDER_PAGE, {"results": []}], ids=["results", "empty-results"])
@pytest.mark.parametrize(
    "answer",
    [
        ([], [{"error_code": "NO_MATCHING_RESULT", "reasoning": "No result fits.", "confidence_float": 0.9}]),
        ([], []),
        (["result"], []),
    ],
)
async def test_search_prompt_match_outcome(
    search_setup: tuple[WebSearchBlock, WorkflowRunContext, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    page: dict[str, Any],
    answer: tuple[list[str], list[dict[str, Any]]],
) -> None:
    block, context, handler = search_setup
    block.no_match_error_code = "NO_MATCHING_RESULT"
    block.json_schema = {"type": "array", "items": {"type": "string"}}
    monkeypatch.setattr(WebSearchBlock, "_request", AsyncMock(return_value=page))
    handler.side_effect = [answer[0], {"reasoning": "Checked results.", "errors": answer[1]}]

    result = await block.execute("workflow-run-test", "block-run-test", "org-test")

    output = result.output_parameter_value
    assert output["prompt_output"] == answer[0]
    assert context.values["search_output"] == output
    if not answer[1]:
        assert result.status == BlockStatus.completed
        assert result.success is True
        assert set(output) == {"query", "provider", "results", "total_count", "prompt_output", "raw_response"}
    else:
        assert result.status == BlockStatus.terminated
        assert result.success is False
        assert result.error_codes == ["NO_MATCHING_RESULT"]
        assert result.failure_reason == "No result fits."
        assert output["status"] == "terminated"
        assert output["errors"][0]["error_code"] == "NO_MATCHING_RESULT"
