from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.transient_page_observer import (
    TRANSIENT_TEXT_BINDING_NAME,
    TRANSIENT_TEXT_MATCH_CONFIDENCE,
    TRANSIENT_TEXT_REASONING_SNIPPET_LIMIT,
    TransientPageTextObserver,
    _append_text_event,
    _format_observed_text_reasoning,
    _has_meaningful_text_overlap,
    match_user_defined_errors_from_transient_text,
)
from tests.unit.helpers import make_organization, make_step, make_task


class _FakePage:
    def __init__(self) -> None:
        self.expose_binding = AsyncMock()
        self.evaluate = AsyncMock()


@pytest.mark.asyncio
async def test_transient_text_observer_reuses_binding_and_hands_off_active_observer() -> None:
    page = _FakePage()
    captured: dict[str, Any] = {}

    async def expose_binding(name: str, callback: Callable[[dict[str, Any], Any], None]) -> None:
        captured["name"] = name
        captured["callback"] = callback

    page.expose_binding.side_effect = expose_binding

    first_observer = TransientPageTextObserver(page)  # type: ignore[arg-type]
    await first_observer.start()
    captured["callback"]({}, {"text": "First transient error", "timestamp_ms": 1})
    await first_observer.stop()

    second_observer = TransientPageTextObserver(page)  # type: ignore[arg-type]
    await second_observer.start()
    captured["callback"]({}, {"text": "Second transient error", "timestamp_ms": 2})
    await second_observer.stop()

    assert captured["name"] == TRANSIENT_TEXT_BINDING_NAME
    page.expose_binding.assert_awaited_once()
    assert [event["text"] for event in first_observer.events] == ["First transient error"]
    assert [event["text"] for event in second_observer.events] == ["Second transient error"]


def test_transient_text_overlap_requires_longer_word_window() -> None:
    assert not _has_meaningful_text_overlap(
        "the file is ready to download now",
        "show an error when the file is ready for review",
    )
    assert _has_meaningful_text_overlap(
        "Example download failure says the generated archive could not be saved",
        "Return this error if the page displays download failure says the generated archive could not be saved",
    )


def test_transient_text_overlap_normalizes_inputs() -> None:
    assert _has_meaningful_text_overlap(
        "DOWNLOAD FAILURE SAYS THE GENERATED ARCHIVE COULD NOT BE SAVED",
        "Return this error if the page displays download failure says the generated archive could not be saved",
    )


def test_append_text_event_omits_absent_metadata() -> None:
    events: list[dict[str, Any]] = []

    _append_text_event(
        events,
        {
            "text": "Download failure says the generated archive could not be saved",
            "tag": None,
            "role": "alert",
        },
    )

    assert events == [
        {
            "tag": None,
            "role": "alert",
            "text": "Download failure says the generated archive could not be saved",
        }
    ]


def test_format_observed_text_reasoning_truncates_snippets() -> None:
    text = "x" * (TRANSIENT_TEXT_REASONING_SNIPPET_LIMIT + 20)

    reasoning = _format_observed_text_reasoning([text])

    assert reasoning == f"{'x' * TRANSIENT_TEXT_REASONING_SNIPPET_LIMIT}..."


def test_match_user_defined_error_from_transient_text_uses_heuristic_confidence() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "data_not_downloadable": (
                "Return this error if the page displays download failure says the generated archive could not be saved"
            ),
        },
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    errors = match_user_defined_errors_from_transient_text(
        task,
        step,
        [{"text": "Example download failure says the generated archive could not be saved"}],
    )

    assert len(errors) == 1
    assert errors[0].error_code == "data_not_downloadable"
    assert errors[0].confidence_float == TRANSIENT_TEXT_MATCH_CONFIDENCE
