"""Tests for how SkyvernFrame handles a tree builder that answers with no element tree."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, ContextManager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import ElementHandle, Frame

from skyvern.exceptions import ElementTreeBuildFailed
from skyvern.webeye.utils.page import JS_FUNCTION_DEFS, SkyvernFrame

ELEMENT = {"id": "AAAB", "tagName": "button"}
SECRET_FRAME_URL = "https://example.test/embedded/callback?code=oauth-secret#id_token=signed-secret"


def _skyvern_frame() -> SkyvernFrame:
    frame = MagicMock(spec=Frame)
    frame.url = SECRET_FRAME_URL
    return SkyvernFrame(frame=frame)


class _EvaluateStub:
    """Stands in for SkyvernFrame.evaluate, answering each buildTreeFromBody call from
    ``build_results`` and recording the expression and budget every call was given."""

    def __init__(self, build_results: list, step_seconds: float = 0.0) -> None:
        self._build_results = build_results
        self._step_seconds = step_seconds
        self.calls: list[tuple[str, float | None, float | None]] = []

    async def __call__(
        self,
        *,
        expression: str,
        timeout_ms: float | None = None,
        deadline: float | None = None,
        **kwargs: object,
    ) -> object:
        self.calls.append((expression, timeout_ms, deadline))
        if self._step_seconds:
            await asyncio.sleep(self._step_seconds)
        # The injected bundle also contains "buildTreeFromBody" -- as its definition, not a call.
        if expression != JS_FUNCTION_DEFS and "buildTreeFromBody(" in expression:
            return self._build_results.pop(0)
        return None

    @property
    def expressions(self) -> list[str]:
        return [expression for expression, _, _ in self.calls]

    @property
    def budgets(self) -> list[float]:
        return [timeout_ms for _, timeout_ms, _ in self.calls if timeout_ms is not None]

    @property
    def deadlines(self) -> list[float]:
        return [deadline for _, _, deadline in self.calls if deadline is not None]


def _patch(stub: _EvaluateStub) -> ContextManager[object]:
    # The stub itself, not AsyncMock(side_effect=...): an instance with an async __call__ does not
    # satisfy iscoroutinefunction, so AsyncMock would hand the caller an un-awaited coroutine.
    return patch.object(SkyvernFrame, "evaluate", stub)


@pytest.mark.asyncio
async def test_build_tree_from_body_reinjects_and_retries_when_the_page_returns_no_tree() -> None:
    stub = _EvaluateStub([None, [[ELEMENT], [ELEMENT]]])

    with _patch(stub):
        elements, element_tree, _ = await _skyvern_frame().build_tree_from_body(frame_name="AAAB", frame_index=1)

    assert elements == [ELEMENT]
    assert element_tree == [ELEMENT]
    assert JS_FUNCTION_DEFS in stub.expressions, "domUtils.js should be re-injected before the retry"


@pytest.mark.asyncio
async def test_build_tree_from_body_raises_a_named_error_when_the_retry_also_returns_no_tree() -> None:
    stub = _EvaluateStub([None, None])

    with _patch(stub):
        with pytest.raises(ElementTreeBuildFailed) as excinfo:
            await _skyvern_frame().build_tree_from_body(frame_name="AAAB", frame_index=1)

    assert excinfo.value.returned == "NoneType"


@pytest.mark.asyncio
async def test_build_tree_from_body_rejects_a_two_item_result_that_is_not_two_lists() -> None:
    """``[None, None]`` is length two but carries no tree; pop_destination_facts treats a non-list
    as empty, so letting it through would hand callers ``(None, None, {})`` as a successful build."""
    stub = _EvaluateStub([[None, None], [None, None]])

    with _patch(stub):
        with pytest.raises(ElementTreeBuildFailed) as excinfo:
            await _skyvern_frame().build_tree_from_body(frame_name="AAAB", frame_index=1)

    assert "NoneType" in excinfo.value.returned


@pytest.mark.asyncio
async def test_build_tree_from_body_accepts_a_legitimately_empty_frame_without_retrying() -> None:
    stub = _EvaluateStub([[[], []]])

    with _patch(stub):
        elements, element_tree, destinations = await _skyvern_frame().build_tree_from_body(
            frame_name="AAAB", frame_index=1
        )

    assert (elements, element_tree, destinations) == ([], [], {})
    assert JS_FUNCTION_DEFS not in stub.expressions, "an empty frame is a valid answer, not a lost JS world"


@pytest.mark.asyncio
async def test_build_tree_from_body_spends_one_shared_budget_across_the_retry() -> None:
    """Every step -- the flag write, both builds, the re-injection -- draws down one deadline, so a
    stuck frame cannot cost a fresh full timeout per attempt."""
    stub = _EvaluateStub([None, [[ELEMENT], [ELEMENT]]], step_seconds=0.01)

    with _patch(stub):
        await _skyvern_frame().build_tree_from_body(frame_name="AAAB", frame_index=1, timeout_ms=30_000)

    assert stub.budgets == sorted(stub.budgets, reverse=True), "budgets must only ever shrink"
    assert stub.budgets[0] <= 30_000
    assert stub.budgets[-1] <= stub.budgets[0] - 10, "the retry must not get a fresh full timeout"
    assert len(set(stub.deadlines)) == 1, "every build step must pass the same deadline into recovery"


@pytest.mark.asyncio
async def test_build_tree_from_body_does_not_log_the_raw_frame_url() -> None:
    stub = _EvaluateStub([None, [[ELEMENT], [ELEMENT]]])

    with _patch(stub), patch("skyvern.webeye.utils.page.LOG.warning") as warning:
        await _skyvern_frame().build_tree_from_body(frame_name="AAAB", frame_index=1)

    logged = warning.call_args.kwargs["url"]
    assert "oauth-secret" not in logged and "signed-secret" not in logged
    assert logged == "https://example.test/<redacted>"


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda frame: frame.get_incremental_element_tree(),
            id="get_incremental_element_tree",
        ),
        pytest.param(
            lambda frame: frame.build_tree_from_element(starter=MagicMock(spec=ElementHandle), frame="main.frame"),
            id="build_tree_from_element",
        ),
    ],
)
@pytest.mark.asyncio
async def test_builders_without_a_retry_raise_a_named_error_rather_than_unpacking_none(
    build: Callable[[SkyvernFrame], Awaitable[object]],
) -> None:
    """These two deliberately have no re-injection retry, so they must fail loudly on the first
    non-pair instead of unpacking it."""
    with patch.object(SkyvernFrame, "evaluate", AsyncMock(return_value=None)):
        with pytest.raises(ElementTreeBuildFailed):
            await build(_skyvern_frame())
