"""Tests for token counter utility."""

from skyvern.utils.token_counter import approx_count_tokens, count_tokens


class TestCountTokens:
    """Tests for count_tokens function."""

    def test_special_token_text_is_counted_not_raised(self):
        """Customer text can contain a tokenizer's special-token spelling; counting it must not raise."""
        assert count_tokens("before <|endoftext|> after") > count_tokens("before  after")


class TestApproxCountTokens:
    """Tests for approx_count_tokens function (character-based heuristic)."""

    def test_no_tiktoken_encode(self, monkeypatch):
        """The heuristic must not invoke tiktoken encoding regardless of load state."""

        def _boom() -> None:
            raise AssertionError("approx_count_tokens must not load the encoding")

        monkeypatch.setattr("skyvern.utils.token_counter._get_encoding", _boom)
        assert approx_count_tokens("word " * 1000) == 1250
