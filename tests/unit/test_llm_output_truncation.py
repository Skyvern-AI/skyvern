import base64
import io
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from skyvern.forge.sdk.api.llm.api_handler_factory import _halved_reasoning_budget_parameters
from skyvern.forge.sdk.api.llm.exceptions import LLMOutputTruncatedError
from skyvern.forge.sdk.api.llm.utils import (
    is_content_filtered_response,
    is_truncated_response,
    llm_messages_builder,
    llm_messages_builder_with_history,
    parse_api_response,
)


def _sent_image(block: dict[str, Any]) -> tuple[str, bytes]:
    if block["type"] == "image":
        return block["source"]["media_type"], base64.b64decode(block["source"]["data"])
    header, data = block["image_url"]["url"].split(";base64,")
    return header.removeprefix("data:"), base64.b64decode(data)


def _encoded(image: Image.Image, image_format: str, **save_options: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **save_options)
    return buffer.getvalue()


MessagesBuilder = Callable[..., Awaitable[list[dict[str, Any]]]]


@pytest.mark.asyncio
@pytest.mark.parametrize("builder", [llm_messages_builder, llm_messages_builder_with_history])
@pytest.mark.parametrize("message_pattern", ["anthropic", "openai"])
class TestImageBlockMediaType:
    async def _send(self, builder: MessagesBuilder, message_pattern: str, image: bytes) -> tuple[str, bytes]:
        messages = await builder(prompt="read", screenshots=[image], message_pattern=message_pattern)
        return _sent_image(messages[-1]["content"][1])

    async def test_jpeg_is_sent_unchanged(self, builder: MessagesBuilder, message_pattern: str) -> None:
        jpeg = _encoded(Image.new("RGB", (4, 4), "white"), "JPEG")
        assert await self._send(builder, message_pattern, jpeg) == ("image/jpeg", jpeg)

    async def test_multi_frame_tiff_is_refused(self, builder: MessagesBuilder, message_pattern: str) -> None:
        tiff = _encoded(
            Image.new("RGB", (4, 4), "white"), "TIFF", save_all=True, append_images=[Image.new("RGB", (4, 4), "black")]
        )
        with pytest.raises(ValueError, match="more than one frame"):
            await self._send(builder, message_pattern, tiff)

    async def test_16_bit_tiff_is_refused(self, builder: MessagesBuilder, message_pattern: str) -> None:
        tiff = _encoded(Image.new("I;16", (4, 4), 40000), "TIFF")
        with pytest.raises(ValueError, match="8 bits per channel"):
            await self._send(builder, message_pattern, tiff)

    async def test_rgba_tiff_keeps_transparency(self, builder: MessagesBuilder, message_pattern: str) -> None:
        tiff = _encoded(Image.new("RGBA", (4, 4), (0, 0, 0, 0)), "TIFF")
        media_type, sent = await self._send(builder, message_pattern, tiff)
        decoded = Image.open(io.BytesIO(sent))
        assert (media_type, decoded.format) == ("image/png", "PNG")
        assert decoded.convert("RGBA").getpixel((0, 0)) == (0, 0, 0, 0)

    async def test_gif_is_reencoded_to_png(self, builder: MessagesBuilder, message_pattern: str) -> None:
        gif = _encoded(Image.new("RGB", (4, 4), "white"), "GIF")
        media_type, sent = await self._send(builder, message_pattern, gif)
        assert (media_type, Image.open(io.BytesIO(sent)).format) == ("image/png", "PNG")

    async def test_image_past_the_pixel_bound_is_refused(
        self, builder: MessagesBuilder, message_pattern: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("skyvern.forge.sdk.api.llm.utils._MAX_DECODED_IMAGE_PIXELS", 4)
        bmp = _encoded(Image.new("RGB", (4, 4), "white"), "BMP")
        with pytest.raises(ValueError, match="more than 4 pixels"):
            await self._send(builder, message_pattern, bmp)


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


class TestHalvedReasoningBudgetParameters:
    """What arms the truncation retry. Arming it for a request that declares no reasoning budget
    would re-send identical parameters and double the cost of every truncated call for nothing."""

    def test_declared_budget_is_halved_without_touching_the_completion_cap(self) -> None:
        reduced = _halved_reasoning_budget_parameters(
            {
                "max_completion_tokens": 65536,
                "extra_body": {"provider": {"ignore": ["open-inference"]}, "reasoning": {"max_tokens": 49152}},
            }
        )
        assert reduced == {
            "max_completion_tokens": 65536,
            "extra_body": {"provider": {"ignore": ["open-inference"]}, "reasoning": {"max_tokens": 24576}},
        }

    def test_frozen_config_parameters_are_not_mutated(self) -> None:
        extra_body = {"reasoning": {"max_tokens": 49152}}
        parameters = {"extra_body": extra_body}
        _halved_reasoning_budget_parameters(parameters)
        assert extra_body == {"reasoning": {"max_tokens": 49152}}

    @pytest.mark.parametrize(
        "parameters",
        [
            pytest.param({"max_completion_tokens": 65536}, id="no_extra_body"),
            pytest.param({"extra_body": {"provider": {"order": ["cloudflare"]}}}, id="no_reasoning"),
            pytest.param({"extra_body": {"reasoning": {"effort": "high"}}}, id="effort_not_budget"),
            pytest.param({"extra_body": {"reasoning_effort": "high"}}, id="top_level_effort"),
            pytest.param({"extra_body": {"reasoning": {"max_tokens": 1}}}, id="nothing_left_to_halve"),
        ],
    )
    def test_requests_with_no_reducible_budget_are_left_alone(self, parameters: dict[str, Any]) -> None:
        assert _halved_reasoning_budget_parameters(parameters) is None


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
