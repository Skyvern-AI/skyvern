"""Tests for the summarize-output endpoint and helpers."""

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from skyvern.forge import app
from skyvern.forge.sdk.api.llm.exceptions import (
    EmptyLLMResponseError,
    InvalidLLMResponseFormat,
    InvalidLLMResponseType,
    LLMProviderError,
)
from skyvern.forge.sdk.routes.prompts import summarize_output
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.prompts import SummarizeOutputRequest, SummarizeOutputResponse
from skyvern.utils.strings import escape_code_fences
from tests.unit.helpers import make_organization


class TestEscapeCodeFences:
    def test_none_returns_empty_string(self) -> None:
        assert escape_code_fences(None) == ""

    def test_triple_backticks_are_neutralized(self) -> None:
        assert escape_code_fences("a ```evil``` b") == "a ` ` `evil` ` ` b"

    def test_triple_tildes_are_neutralized(self) -> None:
        assert escape_code_fences("a ~~~evil~~~ b") == "a ~ ~ ~evil~ ~ ~ b"

    def test_fullwidth_backticks_normalized_then_escaped(self) -> None:
        # U+FF40 is fullwidth grave accent; NFKC normalizes it to `
        assert "```" not in escape_code_fences("\uff40\uff40\uff40")


class TestSummarizeOutputRequest:
    def test_valid_json_accepted(self) -> None:
        req = SummarizeOutputRequest(output_json='{"a": 1}')
        assert req.output_json == '{"a": 1}'

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SummarizeOutputRequest(output_json="not json")

    def test_empty_output_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SummarizeOutputRequest(output_json="")

    def test_oversized_output_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SummarizeOutputRequest(output_json='"' + "x" * 100_001 + '"')

    def test_oversized_title_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SummarizeOutputRequest(output_json="{}", workflow_title="x" * 501)

    def test_deeply_nested_json_rejected(self) -> None:
        # 10k levels of nesting exceeds Python's recursion limit inside json.loads.
        deep = "[" * 10_000 + "]" * 10_000
        with pytest.raises(ValidationError):
            SummarizeOutputRequest(output_json=deep)


def _fake_org() -> Organization:
    return make_organization(datetime.now(timezone.utc))


@contextmanager
def _patch_llm_handler(handler: AsyncMock) -> Iterator[None]:
    """Temporarily install an LLM handler on the app for a single test."""
    sentinel = object()
    original = getattr(app, "LLM_API_HANDLER", sentinel)
    app.LLM_API_HANDLER = handler  # type: ignore[attr-defined]
    try:
        yield
    finally:
        if original is sentinel:
            delattr(app, "LLM_API_HANDLER")
        else:
            app.LLM_API_HANDLER = original  # type: ignore[attr-defined]


@pytest.mark.asyncio
class TestSummarizeOutputRoute:
    async def test_success_returns_stripped_summary(self) -> None:
        with _patch_llm_handler(AsyncMock(return_value={"summary": "  a summary  "})):
            result = await summarize_output(
                request=SummarizeOutputRequest(output_json='{"a": 1}'),
                current_org=_fake_org(),
            )
        assert isinstance(result, SummarizeOutputResponse)
        assert result.summary == "a summary"
        assert result.error is None

    async def test_non_dict_response_returns_structured_error(self) -> None:
        with _patch_llm_handler(AsyncMock(return_value="just a string")):
            result = await summarize_output(
                request=SummarizeOutputRequest(output_json='{"a": 1}'),
                current_org=_fake_org(),
            )
        assert result.summary == ""
        assert result.error == "LLM response is not valid JSON."

    async def test_missing_summary_key_returns_structured_error(self) -> None:
        with _patch_llm_handler(AsyncMock(return_value={"other_field": "x"})):
            result = await summarize_output(
                request=SummarizeOutputRequest(output_json='{"a": 1}'),
                current_org=_fake_org(),
            )
        assert result.summary == ""
        assert result.error == "LLM response missing 'summary' field."

    async def test_non_string_summary_returns_structured_error(self) -> None:
        with _patch_llm_handler(AsyncMock(return_value={"summary": 42})):
            result = await summarize_output(
                request=SummarizeOutputRequest(output_json='{"a": 1}'),
                current_org=_fake_org(),
            )
        assert result.summary == ""
        assert result.error == "LLM 'summary' field is not a string."

    @pytest.mark.parametrize(
        "exc",
        [
            InvalidLLMResponseFormat("bad"),
            InvalidLLMResponseType("list"),
            EmptyLLMResponseError("empty"),
        ],
    )
    async def test_malformed_llm_output_returns_structured_error(self, exc: Exception) -> None:
        with _patch_llm_handler(AsyncMock(side_effect=exc)):
            result = await summarize_output(
                request=SummarizeOutputRequest(output_json='{"a": 1}'),
                current_org=_fake_org(),
            )
        assert result.summary == ""
        assert result.error == "LLM response is not valid JSON."

    async def test_llm_provider_error_raises_503(self) -> None:
        with _patch_llm_handler(AsyncMock(side_effect=LLMProviderError("down"))):
            with pytest.raises(HTTPException) as exc_info:
                await summarize_output(
                    request=SummarizeOutputRequest(output_json='{"a": 1}'),
                    current_org=_fake_org(),
                )
        assert exc_info.value.status_code == 503

    async def test_unexpected_exception_raises_500(self) -> None:
        with _patch_llm_handler(AsyncMock(side_effect=RuntimeError("boom"))):
            with pytest.raises(HTTPException) as exc_info:
                await summarize_output(
                    request=SummarizeOutputRequest(output_json='{"a": 1}'),
                    current_org=_fake_org(),
                )
        assert exc_info.value.status_code == 500
