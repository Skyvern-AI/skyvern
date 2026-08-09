"""Tests for the Vertex cachedContents minimum-size guard (SKY-9983 / SKY-10043)."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.api.llm.vertex_cache_manager import VertexCacheManager, min_cached_content_tokens
from skyvern.utils.token_counter import count_tokens

SMALL_CONTENT = "tiny static prompt"
MID_CONTENT = "word " * 3000  # between the 2.x and 3.x minimums
BIG_CONTENT = "word " * 5000  # above every minimum


@pytest.fixture(autouse=True)
def _token_size_sanity() -> None:
    assert count_tokens(SMALL_CONTENT) < 2048
    assert 2048 <= count_tokens(MID_CONTENT) < 4096
    assert count_tokens(BIG_CONTENT) >= 4096


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> VertexCacheManager:
    mgr = VertexCacheManager(project_id="test-project")
    monkeypatch.setattr(VertexCacheManager, "_get_access_token", lambda self: "test-token")
    return mgr


def _mock_post(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"name": "cachedContents/abc", "expireTime": "2099-01-01T00:00:00Z"}
    post = MagicMock(return_value=response)
    monkeypatch.setattr("skyvern.forge.sdk.api.llm.vertex_cache_manager.requests.post", post)
    return post


class TestMinCachedContentTokens:
    @pytest.mark.parametrize(
        ("model_name", "expected"),
        [
            ("gemini-1.5-flash", 32769),
            ("gemini-1.5-pro", 32769),
            ("vertex_ai/gemini-1.5-pro", 32769),
            ("gemini-2.5-flash", 2048),
            ("gemini-2.5-flash-lite", 2048),
            ("gemini-2.5-pro", 2048),
            ("gemini-3-flash-preview", 4096),
            ("gemini-3.1-flash-lite", 4096),
            ("some-unknown-model", 4096),
        ],
    )
    def test_per_model_minimums(self, model_name: str, expected: int) -> None:
        assert min_cached_content_tokens(model_name) == expected


class TestCreateCacheGuard:
    def test_skips_below_minimum_without_any_api_call(
        self, manager: VertexCacheManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        post = _mock_post(monkeypatch)
        result = manager.create_cache(model_name="gemini-3.1-flash-lite", static_content=SMALL_CONTENT, cache_key="k1")
        assert result is None
        post.assert_not_called()
        assert "k1" not in manager._cache_registry

    def test_ga_minimum_regression_case(self, manager: VertexCacheManager, monkeypatch: pytest.MonkeyPatch) -> None:
        """The SKY-10043 incident shape: content that satisfies the 2.x minimum but not the 3.x GA one."""
        post = _mock_post(monkeypatch)
        skipped = manager.create_cache(model_name="gemini-3.1-flash-lite", static_content=MID_CONTENT, cache_key="k-3x")
        assert skipped is None
        post.assert_not_called()

        created: dict[str, Any] | None = manager.create_cache(
            model_name="gemini-2.5-flash", static_content=MID_CONTENT, cache_key="k-2x"
        )
        assert created is not None
        assert created["name"] == "cachedContents/abc"
        post.assert_called_once()

    def test_gemini_1x_minimum_not_conflated_with_2x(
        self, manager: VertexCacheManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Content above the 2.x/3.x floors is still below Gemini 1.5's 32,769-token minimum."""
        post = _mock_post(monkeypatch)
        result = manager.create_cache(model_name="gemini-1.5-pro", static_content=BIG_CONTENT, cache_key="k-1x")
        assert result is None
        post.assert_not_called()

    def test_creates_above_minimum(self, manager: VertexCacheManager, monkeypatch: pytest.MonkeyPatch) -> None:
        post = _mock_post(monkeypatch)
        result = manager.create_cache(model_name="gemini-3.1-flash-lite", static_content=BIG_CONTENT, cache_key="k2")
        assert result is not None
        assert manager._cache_registry["k2"]["name"] == "cachedContents/abc"
        post.assert_called_once()

    def test_system_instruction_counts_toward_minimum(
        self, manager: VertexCacheManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        post = _mock_post(monkeypatch)
        result = manager.create_cache(
            model_name="gemini-3.1-flash-lite",
            static_content=MID_CONTENT,
            cache_key="k3",
            system_instruction=MID_CONTENT,
        )
        assert result is not None
        post.assert_called_once()
