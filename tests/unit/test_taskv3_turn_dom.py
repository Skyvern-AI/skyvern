import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.taskv3 import turn_dom
from skyvern.forge.taskv3.turn_dom import TurnDomCapture

_TURN_STEP: Any = SimpleNamespace(step_id="stp_turn")


class _Store:
    def __init__(self, raises: BaseException | None = None) -> None:
        self.saved: list[tuple[str, bytes]] = []
        self.called = asyncio.Event()
        self._raises = raises

    def __call__(self, step: Any, html: bytes) -> str:
        self.called.set()
        if self._raises is not None:
            raise self._raises
        self.saved.append((step.step_id, html))
        return f"a_{len(self.saved)}"


def _pages(*htmls: str | None) -> Any:
    queue = list(htmls)

    async def read_page() -> str | None:
        return queue.pop(0)

    return read_page


class _GatedPage:
    def __init__(self, html: str) -> None:
        self.html = html
        self.reading = asyncio.Event()
        self.release = asyncio.Event()
        self.stopped = asyncio.Event()

    async def __call__(self) -> str:
        self.reading.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.stopped.set()
            raise
        return self.html


@pytest.mark.asyncio
async def test_a_capture_that_finishes_during_the_model_call_costs_the_turn_no_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(turn_dom, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    store = _Store()
    capture = TurnDomCapture(_pages("<html>a</html>"), store)

    capture.start(_TURN_STEP, 1)
    await store.called.wait()
    clock[0] = 10.0
    await capture.wait()

    page_dom = capture.turn_output()
    assert page_dom is not None and page_dom.status == "stored" and page_dom.artifact_id == "a_1"
    assert store.saved == [("stp_turn", b"<html>a</html>")]
    totals = capture.totals()
    assert (totals["dom_captures"], totals["dom_stored"], totals["dom_wait_seconds"]) == (1, 1, 0.0)


@pytest.mark.asyncio
async def test_a_hanging_capture_is_cut_at_its_cap_and_stores_nothing() -> None:
    store = _Store()
    page = _GatedPage("<p>never</p>")
    capture = TurnDomCapture(page, store, cap_seconds=0.05)
    capture.start(_TURN_STEP, 1)
    await capture.wait()

    page_dom = capture.turn_output()
    assert page_dom is not None and (page_dom.status, page_dom.reason) == ("skipped", "TimeoutError")
    assert page.stopped.is_set() and store.saved == []


@pytest.mark.asyncio
async def test_an_unchanged_page_stores_a_reference_to_the_earlier_copy() -> None:
    store = _Store()
    capture = TurnDomCapture(_pages("<p>x</p>", "<p>x</p>", "<p>y</p>"), store)
    outputs: list[Any] = []
    for turn in (1, 2, 3):
        capture.start(_TURN_STEP, turn)
        await capture.wait()
        outputs.append(capture.turn_output())

    assert [(o.status, o.artifact_id) for o in outputs] == [("stored", "a_1"), ("deduped", "a_1"), ("stored", "a_2")]
    assert [html for _, html in store.saved] == [b"<p>x</p>", b"<p>y</p>"]
    totals = capture.totals()
    assert (totals["dom_stored"], totals["dom_deduped"]) == (2, 1)


@pytest.mark.parametrize(
    ("read_page", "store", "step", "reason"),
    [
        (_pages(None), _Store(), _TURN_STEP, "no_working_page"),
        (_pages(), _Store(), _TURN_STEP, "IndexError"),
        (_pages("<p>x</p>"), _Store(raises=RuntimeError("redaction failed")), _TURN_STEP, "RuntimeError"),
        # The block step's archive keys files by name, so a second page there would overwrite the first.
        (_pages("<p>x</p>"), _Store(), None, "no_turn_step"),
    ],
)
@pytest.mark.asyncio
async def test_a_failing_capture_is_skipped_with_its_reason_and_never_raises(
    read_page: Any, store: _Store, step: Any, reason: str
) -> None:
    capture = TurnDomCapture(read_page, store)
    capture.start(step, 1)
    await capture.wait()

    page_dom = capture.turn_output()
    assert page_dom is not None and (page_dom.status, page_dom.reason) == ("skipped", reason)
    assert capture.totals()["dom_skipped"] == 1
    assert store.saved == []


@pytest.mark.asyncio
async def test_cancel_stops_a_capture_whose_turn_never_reached_the_page() -> None:
    store = _Store()
    page = _GatedPage("<p>x</p>")
    capture = TurnDomCapture(page, store)
    capture.start(_TURN_STEP, 1)
    await page.reading.wait()
    capture.cancel()
    await page.stopped.wait()
    await capture.wait()

    assert store.saved == []
    assert capture.turn_output() is None
    assert capture.totals()["dom_captures"] == 0


@pytest.mark.asyncio
async def test_a_turn_that_ends_without_the_join_still_reports_a_finished_capture_and_stops_a_running_one() -> None:
    # A failed model call ends the turn before the join: a capture already done is recorded, one still
    # running is stopped so it cannot store into an archive the turn has already handed off.
    store = _Store()
    capture = TurnDomCapture(_pages("<p>x</p>"), store)
    capture.start(_TURN_STEP, 1)
    await store.called.wait()
    page_dom = capture.turn_output()
    assert page_dom is not None and page_dom.status == "stored"
    assert capture.totals()["dom_stored"] == 1

    page = _GatedPage("<p>y</p>")
    slow = TurnDomCapture(page, store)
    slow.start(_TURN_STEP, 1)
    await page.reading.wait()
    assert slow.turn_output() is None
    await page.stopped.wait()
    assert [html for _, html in store.saved] == [b"<p>x</p>"]
