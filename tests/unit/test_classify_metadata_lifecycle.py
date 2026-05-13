"""Tests for ``_store_classify_result`` metadata lifecycle on SkyvernContext
and the consume-at-entry invariant in ``ai_element_fallback``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.core.script_generations.real_skyvern_page_ai import RealSkyvernPageAi
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context


@pytest.fixture
def ctx() -> skyvern_context.SkyvernContext:
    skyvern_context.set(skyvern_context.SkyvernContext(organization_id="o_test", workflow_run_id="wr_test"))
    yield skyvern_context.current()
    skyvern_context.reset()


def test_unknown_writes_full_meta(ctx: skyvern_context.SkyvernContext) -> None:
    RealSkyvernPageAi._store_classify_result(
        "UNKNOWN",
        current_url="https://example.com/search",
        options={"card_link": "single recipe card"},
        block_label="search_block",
        rejection_reasoning="page is search results, not a single card",
        confidence=1.0,
        text_excerpt="Recipe results for...",
    )
    meta = ctx.last_classify_meta
    assert meta is not None
    assert meta["result"] == "UNKNOWN"
    assert meta["url_at_classify"] == "https://example.com/search"
    assert meta["block_label_at_classify"] == "search_block"
    assert meta["candidate_options"] == {"card_link": "single recipe card"}
    assert meta["rejection_reasoning"] == "page is search results, not a single card"
    assert meta["confidence"] == 1.0
    assert meta["text_excerpt"] == "Recipe results for..."
    assert ctx.last_classify_result == "UNKNOWN"


def test_matched_result_clears_meta(ctx: skyvern_context.SkyvernContext) -> None:
    ctx.last_classify_meta = {"result": "UNKNOWN", "stale": True}
    RealSkyvernPageAi._store_classify_result(
        "card_link",
        current_url="https://example.com/recipe/123",
        options={"card_link": "single recipe card"},
        block_label="search_block",
    )
    assert ctx.last_classify_meta is None
    assert ctx.last_classify_result == "card_link"


def test_unknown_with_default_args_does_not_raise(ctx: skyvern_context.SkyvernContext) -> None:
    """LLM-exception path: reasoning='', confidence=0.0, text_excerpt=''."""
    RealSkyvernPageAi._store_classify_result(
        "UNKNOWN",
        current_url="https://example.com",
        options={"a": "x"},
        block_label=None,
    )
    meta = ctx.last_classify_meta
    assert meta is not None
    assert meta["rejection_reasoning"] == ""
    assert meta["confidence"] == 0.0
    assert meta["text_excerpt"] == ""
    assert meta["block_label_at_classify"] is None


def test_text_excerpt_is_truncated_to_500(ctx: skyvern_context.SkyvernContext) -> None:
    long_text = "x" * 1000
    RealSkyvernPageAi._store_classify_result(
        "UNKNOWN",
        current_url="https://example.com",
        options={"a": "x"},
        block_label="b",
        text_excerpt=long_text,
    )
    assert ctx.last_classify_meta is not None
    assert len(ctx.last_classify_meta["text_excerpt"]) == 500


def test_no_context_no_raise() -> None:
    """When there's no current SkyvernContext, the helper is a silent no-op."""
    RealSkyvernPageAi._store_classify_result(
        "UNKNOWN",
        current_url="https://example.com",
        options={"a": "x"},
        block_label=None,
    )


def _make_page_stub(url: str = "https://example.com/x") -> SimpleNamespace:
    async def _inner_text(_selector: str) -> str:
        return "page text"

    return SimpleNamespace(url=url, inner_text=_inner_text)


def _make_page_ai_stub(page: SimpleNamespace) -> RealSkyvernPageAi:
    inst = RealSkyvernPageAi.__new__(RealSkyvernPageAi)
    inst.page = page  # type: ignore[assignment]
    inst.scraped_page = SimpleNamespace()  # type: ignore[assignment]
    inst.current_label = "block_a"
    return inst


@pytest.fixture
def fallback_ctx() -> skyvern_context.SkyvernContext:
    skyvern_context.set(
        skyvern_context.SkyvernContext(
            organization_id="o_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
        )
    )
    yield skyvern_context.current()
    skyvern_context.reset()


@pytest.mark.asyncio
async def test_ai_element_fallback_consumes_meta_on_success(
    fallback_ctx: skyvern_context.SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: meta is popped at function entry; subsequent reads see None."""
    fallback_ctx.last_classify_meta = {
        "result": "UNKNOWN",
        "url_at_classify": "https://example.com/x",
        "block_label_at_classify": "block_a",
        "candidate_options": {"a": "x"},
        "rejection_reasoning": "n/a",
        "confidence": 1.0,
        "text_excerpt": "",
    }
    page = _make_page_stub()
    page_ai = _make_page_ai_stub(page)

    monkeypatch.setattr(page_ai, "ai_validate", AsyncMock(return_value=True))
    monkeypatch.setattr(page_ai, "ai_act", AsyncMock(return_value=None))
    monkeypatch.setattr(app.DATABASE.scripts, "create_fallback_episode", AsyncMock(return_value=None))

    await page_ai.ai_element_fallback(navigation_goal="real goal")

    assert fallback_ctx.last_classify_meta is None


@pytest.mark.asyncio
async def test_ai_element_fallback_consumes_meta_when_ai_act_raises(
    fallback_ctx: skyvern_context.SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exception path: meta is popped at function entry, persists None even when ai_act raises mid-loop."""
    fallback_ctx.last_classify_meta = {
        "result": "UNKNOWN",
        "url_at_classify": "https://example.com/x",
        "block_label_at_classify": "block_a",
        "candidate_options": {"a": "x"},
        "rejection_reasoning": "n/a",
        "confidence": 1.0,
        "text_excerpt": "",
    }
    page = _make_page_stub()
    page_ai = _make_page_ai_stub(page)

    monkeypatch.setattr(page_ai, "ai_validate", AsyncMock(return_value=False))
    monkeypatch.setattr(page_ai, "ai_act", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(app.DATABASE.scripts, "create_fallback_episode", AsyncMock(return_value=None))

    with pytest.raises(RuntimeError, match="boom"):
        await page_ai.ai_element_fallback(navigation_goal="real goal", max_steps=2)

    assert fallback_ctx.last_classify_meta is None


@pytest.mark.asyncio
async def test_ai_element_fallback_skips_validate_on_first_iteration(
    fallback_ctx: skyvern_context.SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caller invokes element_fallback only when the cached path needs help, so
    the goal cannot already be achieved on entry — the iteration-0 validate is
    a guaranteed-False LLM call that should be skipped.
    """
    page = _make_page_stub()
    page_ai = _make_page_ai_stub(page)

    validate_mock = AsyncMock(return_value=True)
    act_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(page_ai, "ai_validate", validate_mock)
    monkeypatch.setattr(page_ai, "ai_act", act_mock)
    monkeypatch.setattr(app.DATABASE.scripts, "create_fallback_episode", AsyncMock(return_value=None))

    await page_ai.ai_element_fallback(navigation_goal="real goal", max_steps=3)

    assert act_mock.await_count == 1
    assert validate_mock.await_count == 1


@pytest.mark.asyncio
async def test_ai_element_fallback_validates_after_act_on_subsequent_iterations(
    fallback_ctx: skyvern_context.SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the first act, every iteration validates before acting again so a
    completed goal short-circuits the loop without a stale extra act. The
    relative call ordering (act → validate → act → validate, then break) is
    asserted against a parent mock manager to prevent regressions.
    """
    page = _make_page_stub()
    page_ai = _make_page_ai_stub(page)

    parent = MagicMock()
    validate_mock = AsyncMock(side_effect=[False, True])
    act_mock = AsyncMock(return_value=None)
    parent.attach_mock(validate_mock, "validate")
    parent.attach_mock(act_mock, "act")

    monkeypatch.setattr(page_ai, "ai_validate", validate_mock)
    monkeypatch.setattr(page_ai, "ai_act", act_mock)
    monkeypatch.setattr(app.DATABASE.scripts, "create_fallback_episode", AsyncMock(return_value=None))

    await page_ai.ai_element_fallback(navigation_goal="real goal", max_steps=5)

    call_names = [c[0] for c in parent.mock_calls]
    assert call_names == ["act", "validate", "act", "validate"]


@pytest.mark.asyncio
async def test_ai_element_fallback_validate_first_true_preserves_legacy_check(
    fallback_ctx: skyvern_context.SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive callers that may invoke element_fallback on a possibly-complete
    page can opt back into the pre-act validate via ``validate_first=True``;
    when the page already satisfies the goal the loop must exit without acting.
    """
    page = _make_page_stub()
    page_ai = _make_page_ai_stub(page)

    validate_mock = AsyncMock(return_value=True)
    act_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(page_ai, "ai_validate", validate_mock)
    monkeypatch.setattr(page_ai, "ai_act", act_mock)
    monkeypatch.setattr(app.DATABASE.scripts, "create_fallback_episode", AsyncMock(return_value=None))

    await page_ai.ai_element_fallback(navigation_goal="real goal", max_steps=3, validate_first=True)

    assert act_mock.await_count == 0
    assert validate_mock.await_count == 1


@pytest.mark.asyncio
async def test_ai_element_fallback_max_steps_one_can_succeed(
    fallback_ctx: skyvern_context.SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``max_steps=1`` must be able to succeed when the single act completes the
    goal — guaranteed by a final post-loop validate that runs when the loop
    exhausts without an early break.
    """
    page = _make_page_stub()
    page_ai = _make_page_ai_stub(page)

    validate_mock = AsyncMock(return_value=True)
    act_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(page_ai, "ai_validate", validate_mock)
    monkeypatch.setattr(page_ai, "ai_act", act_mock)
    monkeypatch.setattr(app.DATABASE.scripts, "create_fallback_episode", AsyncMock(return_value=None))

    await page_ai.ai_element_fallback(navigation_goal="real goal", max_steps=1)

    assert act_mock.await_count == 1
    assert validate_mock.await_count == 1


@pytest.mark.asyncio
async def test_ai_element_fallback_raises_when_max_steps_exhausted_without_completion(
    fallback_ctx: skyvern_context.SkyvernContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If every act runs and the final validate still says incomplete, the
    method raises rather than silently succeeding.
    """
    page = _make_page_stub()
    page_ai = _make_page_ai_stub(page)

    validate_mock = AsyncMock(return_value=False)
    act_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(page_ai, "ai_validate", validate_mock)
    monkeypatch.setattr(page_ai, "ai_act", act_mock)
    monkeypatch.setattr(app.DATABASE.scripts, "create_fallback_episode", AsyncMock(return_value=None))

    with pytest.raises(Exception, match="did not complete within 2 steps"):
        await page_ai.ai_element_fallback(navigation_goal="real goal", max_steps=2)

    assert act_mock.await_count == 2
    assert validate_mock.await_count == 2
