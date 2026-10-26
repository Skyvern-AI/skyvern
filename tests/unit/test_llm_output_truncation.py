import base64
import io
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from skyvern.forge.sdk.api.llm import utils as llm_utils
from skyvern.forge.sdk.api.llm.api_handler_factory import _halved_reasoning_budget_parameters
from skyvern.forge.sdk.api.llm.exceptions import LLMOutputTruncatedError
from skyvern.forge.sdk.api.llm.utils import (
    is_content_filtered_response,
    is_truncated_response,
    llm_messages_builder,
    llm_messages_builder_with_history,
    normalize_llm_image,
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


def _multi_page_group4_tiff(page_count: int = 3) -> bytes:
    pages = [Image.new("1", (4, 4), page % 2) for page in range(page_count)]
    return _encoded(pages[0], "TIFF", save_all=True, append_images=pages[1:], compression="group4")


def _multi_frame_gif() -> bytes:
    frames = [Image.new("RGB", (4, 4), color) for color in ("white", "black")]
    return _encoded(frames[0], "GIF", save_all=True, append_images=frames[1:])


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

    async def test_multi_frame_tiff_is_sent_as_png_images(self, builder: MessagesBuilder, message_pattern: str) -> None:
        messages = await builder(
            prompt="read", screenshots=[_multi_page_group4_tiff()], message_pattern=message_pattern
        )
        image_blocks = [block for block in messages[-1]["content"] if block["type"] in ("image", "image_url")]
        sent_images = [_sent_image(block) for block in image_blocks]

        assert len(sent_images) == 3
        assert all(media_type == "image/png" for media_type, _ in sent_images)
        assert all(Image.open(io.BytesIO(image)).format == "PNG" for _, image in sent_images)
        page_pixels = [Image.open(io.BytesIO(image)).convert("RGBA").getpixel((0, 0)) for _, image in sent_images]
        assert page_pixels[0] != page_pixels[1]

    async def test_multi_frame_tiff_over_frame_limit_is_refused(
        self, builder: MessagesBuilder, message_pattern: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("skyvern.forge.sdk.api.llm.utils._MAX_DECODED_IMAGE_FRAMES", 2)
        with pytest.raises(ValueError, match="more than 2 frames"):
            await builder(prompt="read", screenshots=[_multi_page_group4_tiff()], message_pattern=message_pattern)

    async def test_multi_frame_tiff_over_default_frame_limit_is_refused(
        self, builder: MessagesBuilder, message_pattern: str
    ) -> None:
        with pytest.raises(ValueError, match="more than 20 frames"):
            await builder(
                prompt="read", screenshots=[_multi_page_group4_tiff(page_count=21)], message_pattern=message_pattern
            )

    async def test_animated_gif_is_refused(self, builder: MessagesBuilder, message_pattern: str) -> None:
        with pytest.raises(ValueError, match="more than one frame"):
            await builder(prompt="read", screenshots=[_multi_frame_gif()], message_pattern=message_pattern)

    async def test_multi_frame_tiff_over_total_pixel_bound_is_refused(
        self, builder: MessagesBuilder, message_pattern: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("skyvern.forge.sdk.api.llm.utils._MAX_TOTAL_DECODED_IMAGE_PIXELS", 20)
        with pytest.raises(ValueError, match="more than 20 pixels across frames"):
            await builder(prompt="read", screenshots=[_multi_page_group4_tiff()], message_pattern=message_pattern)

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


@pytest.mark.asyncio
async def test_fourteen_page_tiff_fits_fax_pixel_budget() -> None:
    assert llm_utils._MAX_DECODED_IMAGE_FRAMES == 20
    assert llm_utils._MAX_TOTAL_DECODED_IMAGE_PIXELS >= 14 * 1728 * 2200
    normalized = await normalize_llm_image(_multi_page_group4_tiff(page_count=14))
    assert isinstance(normalized, list)
    assert len(normalized) == 14


@pytest.mark.asyncio
@pytest.mark.parametrize("builder", [llm_messages_builder, llm_messages_builder_with_history])
@pytest.mark.parametrize("message_pattern", ["anthropic", "openai"])
async def test_request_rejects_more_than_twenty_image_blocks(builder: MessagesBuilder, message_pattern: str) -> None:
    image = _encoded(Image.new("RGB", (4, 4), "white"), "PNG")
    with pytest.raises(ValueError, match="more than 20 image blocks"):
        await builder(prompt="read", screenshots=[image] * 21, message_pattern=message_pattern)


@pytest.mark.asyncio
@pytest.mark.parametrize("message_pattern", ["anthropic", "openai"])
async def test_history_prunes_old_image_blocks_before_appending_tiff_pages(message_pattern: str) -> None:
    image = _encoded(Image.new("RGB", (4, 4), "white"), "PNG")
    encoded = base64.b64encode(image).decode("utf-8")
    image_block = (
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": encoded},
        }
        if message_pattern == "anthropic"
        else {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
    )
    messages = await llm_messages_builder_with_history(
        prompt="read current pages",
        screenshots=[_multi_page_group4_tiff(page_count=20)],
        message_history=[{"role": "user", "content": [{"type": "text", "text": "earlier context"}, image_block]}],
        message_pattern=message_pattern,
    )

    image_blocks = [
        block
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") in ("image", "image_url")
    ]
    assert len(image_blocks) == 20
    assert messages[0]["content"] == [{"type": "text", "text": "earlier context"}]


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
