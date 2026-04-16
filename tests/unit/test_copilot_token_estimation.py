"""Tests for estimate_tokens and _sanitize_for_token_estimation."""

from skyvern.forge.sdk.copilot.enforcement import (
    TOKENS_PER_RESIZED_IMAGE,
    _sanitize_for_token_estimation,
    estimate_tokens,
)


class TestEstimateTokens:
    def test_empty_input(self):
        assert estimate_tokens([]) == 0

    def test_plain_text_items(self):
        items = [
            {"role": "user", "content": "Hello, how are you?"},
            {"role": "assistant", "content": "I am fine, thanks."},
        ]
        result = estimate_tokens(items)
        assert result > 0
        assert isinstance(result, int)

    def test_image_estimate_independent_of_base64_size(self):
        """Tiny vs huge base64 must produce the exact same estimate."""
        small = [
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,abc",
                "detail": "high",
            }
        ]
        large = [
            {
                "type": "input_image",
                "image_url": "data:image/png;base64," + "A" * 200_000,
                "detail": "high",
            }
        ]
        assert estimate_tokens(small) == estimate_tokens(large)

    def test_mixed_text_and_images(self):
        items = [
            {"role": "user", "content": "Describe this image."},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Here is a screenshot."},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64," + "A" * 10000,
                        "detail": "high",
                    },
                ],
            },
        ]
        result = estimate_tokens(items)
        assert result >= TOKENS_PER_RESIZED_IMAGE
        assert result > 10

    def test_base64_excluded_from_text_tokens(self):
        """The text-token component must not include base64 data.

        We subtract the flat image cost to isolate the text-token portion
        and verify it stays small regardless of image_url size.
        """
        items = [
            {
                "type": "input_image",
                "image_url": "data:image/png;base64," + "A" * 200_000,
                "detail": "high",
            }
        ]
        total = estimate_tokens(items)
        text_portion = total - TOKENS_PER_RESIZED_IMAGE
        # The sanitized JSON is ~60 chars ('[{"type":"input_image","image_url":"[image]","detail":"high"}]')
        # so text tokens should be well under 100.
        assert text_portion < 100

    def test_image_metadata_preserved(self):
        """type and detail fields should be included, only image_url replaced."""
        item = {
            "type": "input_image",
            "image_url": "data:image/png;base64," + "X" * 1000,
            "detail": "high",
        }
        sanitized, count = _sanitize_for_token_estimation(item)
        assert count == 1
        assert sanitized["type"] == "input_image"
        assert sanitized["detail"] == "high"
        assert sanitized["image_url"] == "[image]"

    def test_nested_dict_structures(self):
        items = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "outer"},
                    {"nested": {"deep": {"value": "hello"}}},
                ],
            }
        ]
        result = estimate_tokens(items)
        assert result > 0

    def test_structure_overhead(self):
        """Structured payload should estimate higher than leaf strings alone."""
        leaf_text = "hello world"
        structured = [{"key1": leaf_text, "key2": {"nested_key": leaf_text}}]
        plain = [leaf_text]
        structured_tokens = estimate_tokens(structured)
        plain_tokens = estimate_tokens(plain)
        assert structured_tokens > plain_tokens

    def test_non_serializable_object(self):
        """Custom objects should not raise; handled via str() fallback."""

        class Custom:
            def __str__(self) -> str:
                return "custom-object-repr"

        items = [{"data": Custom(), "text": "normal"}]
        result = estimate_tokens(items)
        assert result > 0
