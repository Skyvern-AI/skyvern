from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.api.llm.exceptions import LLMOutputTruncatedError
from skyvern.forge.sdk.api.llm.utils import (
    is_content_filtered_response,
    is_truncated_response,
    parse_api_response,
)


def _make_response(finish_reason: str, content: str | None, model: str = "gemini-3-flash-preview") -> MagicMock:
    """Build a minimal litellm.ModelResponse mock."""
    choice = MagicMock()
    choice.finish_reason = finish_reason
    choice.message.content = content

    usage = MagicMock()
    usage.prompt_tokens = 64000
    usage.completion_tokens = 65000
    usage.completion_tokens_details.reasoning_tokens = 62000

    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    resp.model = model
    return resp


class TestIsTruncatedResponse:
    def test_length_finish_with_none_content(self) -> None:
        resp = _make_response(finish_reason="length", content=None)
        assert is_truncated_response(resp) is True

    def test_length_finish_with_content(self) -> None:
        resp = _make_response(finish_reason="length", content='{"partial": true}')
        assert is_truncated_response(resp) is False

    def test_stop_finish_with_content(self) -> None:
        resp = _make_response(finish_reason="stop", content='{"result": "ok"}')
        assert is_truncated_response(resp) is False

    def test_stop_finish_with_none_content(self) -> None:
        resp = _make_response(finish_reason="stop", content=None)
        assert is_truncated_response(resp) is False

    def test_empty_choices(self) -> None:
        resp = MagicMock()
        resp.choices = []
        assert is_truncated_response(resp) is False


class TestIsContentFilteredResponse:
    def test_content_filter_finish_with_none_content(self) -> None:
        resp = _make_response(finish_reason="content_filter", content=None)
        assert is_content_filtered_response(resp) is True

    def test_content_filter_finish_with_content(self) -> None:
        resp = _make_response(finish_reason="content_filter", content='{"ok": true}')
        assert is_content_filtered_response(resp) is False

    def test_stop_finish_with_none_content(self) -> None:
        resp = _make_response(finish_reason="stop", content=None)
        assert is_content_filtered_response(resp) is False

    def test_length_finish_with_none_content(self) -> None:
        resp = _make_response(finish_reason="length", content=None)
        assert is_content_filtered_response(resp) is False

    def test_empty_choices(self) -> None:
        resp = MagicMock()
        resp.choices = []
        assert is_content_filtered_response(resp) is False


class TestParseApiResponseTruncation:
    def test_truncated_response_raises_llm_output_truncated_error(self) -> None:
        resp = _make_response(finish_reason="length", content=None)
        with pytest.raises(LLMOutputTruncatedError) as exc_info:
            parse_api_response(resp)
        assert exc_info.value.model == "gemini-3-flash-preview"
        assert exc_info.value.prompt_tokens == 64000
        assert exc_info.value.reasoning_tokens == 62000

    def test_normal_response_parses_successfully(self) -> None:
        resp = _make_response(finish_reason="stop", content='{"key": "value"}')
        result = parse_api_response(resp)
        assert result == {"key": "value"}

    def test_length_finish_with_partial_content_still_parses(self) -> None:
        resp = _make_response(finish_reason="length", content='{"partial": true}')
        result = parse_api_response(resp, force_dict=False)
        assert result == {"partial": True}
