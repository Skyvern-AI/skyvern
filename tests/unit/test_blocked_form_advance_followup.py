from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from skyvern.webeye.actions import handler
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess


def _element(tag: str, *, attributes: dict | None = None, text: str | None = None) -> dict:
    return {"tagName": tag, "attributes": attributes or {}, "text": text}


@pytest.mark.parametrize(
    ("element", "expected"),
    [
        (_element("button", text="Next"), True),
        (_element("button", text="Continue"), True),
        (_element("button", text="Save and proceed"), True),
        (_element("input", attributes={"type": "submit", "value": "Go"}), True),
        (_element("input", attributes={"type": "image"}), True),
        (_element("a", attributes={"role": "button"}, text="Confirm"), True),
        (_element("button", attributes={"aria-label": "Submit application"}), True),
        (_element("button", text="Add another item"), False),
        (_element("input", attributes={"type": "checkbox"}), False),
        (_element("div", text="Next"), False),
        (_element("a", text="Next"), False),
        (_element("button", text=""), False),
    ],
)
def test_click_is_submit_like(element: dict, expected: bool) -> None:
    assert handler._click_is_submit_like(element) is expected


def _submit_element() -> object:
    return SimpleNamespace(
        get_element_dict=Mock(return_value=_element("button", text="Next")),
        get_id=Mock(return_value="next-btn"),
    )


@pytest.mark.asyncio
async def test_blocked_form_advance_sets_followup_on_non_advancing_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_is_surface_blocked_form_advance_enabled", AsyncMock(return_value=True))
    results: list[object] = [ActionSuccess()]
    await handler._maybe_flag_blocked_form_advance(
        results=results,
        skyvern_element=_submit_element(),
        page=SimpleNamespace(url="https://example.com/step"),
        original_url="https://example.com/step",
        incremental_scraped=SimpleNamespace(get_incremental_elements_num=AsyncMock(return_value=0)),
        task=SimpleNamespace(organization_id=None),
    )
    assert results[-1].needs_followup is True
    assert results[-1].followup_message
    assert "Next" in results[-1].followup_message


@pytest.mark.asyncio
async def test_blocked_form_advance_noop_when_url_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_is_surface_blocked_form_advance_enabled", AsyncMock(return_value=True))
    results: list[object] = [ActionSuccess()]
    await handler._maybe_flag_blocked_form_advance(
        results=results,
        skyvern_element=_submit_element(),
        page=SimpleNamespace(url="https://example.com/next-step"),
        original_url="https://example.com/step",
        incremental_scraped=SimpleNamespace(get_incremental_elements_num=AsyncMock(return_value=0)),
        task=SimpleNamespace(organization_id=None),
    )
    assert results[-1].needs_followup is None


@pytest.mark.asyncio
async def test_blocked_form_advance_noop_when_new_elements_appeared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_is_surface_blocked_form_advance_enabled", AsyncMock(return_value=True))
    results: list[object] = [ActionSuccess()]
    await handler._maybe_flag_blocked_form_advance(
        results=results,
        skyvern_element=_submit_element(),
        page=SimpleNamespace(url="https://example.com/step"),
        original_url="https://example.com/step",
        incremental_scraped=SimpleNamespace(get_incremental_elements_num=AsyncMock(return_value=5)),
        task=SimpleNamespace(organization_id=None),
    )
    assert results[-1].needs_followup is None


@pytest.mark.asyncio
async def test_blocked_form_advance_noop_when_not_submit_like(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_is_surface_blocked_form_advance_enabled", AsyncMock(return_value=True))
    results: list[object] = [ActionSuccess()]
    await handler._maybe_flag_blocked_form_advance(
        results=results,
        skyvern_element=SimpleNamespace(
            get_element_dict=Mock(return_value=_element("input", attributes={"type": "checkbox"})),
            get_id=Mock(return_value="cb"),
        ),
        page=SimpleNamespace(url="https://example.com/step"),
        original_url="https://example.com/step",
        incremental_scraped=SimpleNamespace(get_incremental_elements_num=AsyncMock(return_value=0)),
        task=SimpleNamespace(organization_id=None),
    )
    assert results[-1].needs_followup is None


@pytest.mark.asyncio
async def test_blocked_form_advance_noop_when_flag_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_is_surface_blocked_form_advance_enabled", AsyncMock(return_value=False))
    results: list[object] = [ActionSuccess()]
    await handler._maybe_flag_blocked_form_advance(
        results=results,
        skyvern_element=_submit_element(),
        page=SimpleNamespace(url="https://example.com/step"),
        original_url="https://example.com/step",
        incremental_scraped=SimpleNamespace(get_incremental_elements_num=AsyncMock(return_value=0)),
        task=SimpleNamespace(organization_id=None),
    )
    assert results[-1].needs_followup is None


@pytest.mark.asyncio
async def test_blocked_form_advance_noop_when_last_result_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_is_surface_blocked_form_advance_enabled", AsyncMock(return_value=True))
    results: list[object] = [ActionFailure(Exception("boom"))]
    await handler._maybe_flag_blocked_form_advance(
        results=results,
        skyvern_element=_submit_element(),
        page=SimpleNamespace(url="https://example.com/step"),
        original_url="https://example.com/step",
        incremental_scraped=SimpleNamespace(get_incremental_elements_num=AsyncMock(return_value=0)),
        task=SimpleNamespace(organization_id=None),
    )
    assert results[-1].needs_followup is None
