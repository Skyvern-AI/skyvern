"""Tests for find_deepest_interactable_descendant_in_single_chain.

Single-chain rule: all viable candidates on one ancestor-descendant
chain -> return deepest. Candidates in separate branches -> None.
No id/class/text scoring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.utils.dom import SkyvernElement


def _static(
    *,
    element_id: str,
    interactable: bool,
    disabled: bool = False,
    aria_disabled: str | bool | None = None,
    hover_only: bool = False,
    children: list[dict] | None = None,
) -> dict:
    attrs: dict = {}
    if disabled:
        attrs["disabled"] = True
    if aria_disabled is not None:
        attrs["aria-disabled"] = aria_disabled
    return {
        "id": element_id,
        "tagName": "div",
        "interactable": interactable,
        "hoverOnly": hover_only,
        "attributes": attrs,
        "children": children or [],
    }


def _el(static: dict) -> SkyvernElement:
    return SkyvernElement(MagicMock(), MagicMock(), static)


def test_zero_returns_none() -> None:
    p = _static(element_id="P", interactable=False, disabled=True, children=[])
    assert _el(p).find_deepest_interactable_descendant_in_single_chain() is None


def test_one_returns_it() -> None:
    c = _static(element_id="C", interactable=True)
    p = _static(element_id="P", interactable=False, disabled=True, children=[c])
    assert _el(p).find_deepest_interactable_descendant_in_single_chain() == "C"


def test_two_in_chain_returns_deepest() -> None:
    gc = _static(element_id="GC", interactable=True)
    c = _static(element_id="C", interactable=True, children=[gc])
    p = _static(element_id="P", interactable=False, disabled=True, children=[c])
    assert _el(p).find_deepest_interactable_descendant_in_single_chain() == "GC"


def test_three_in_chain_returns_deepest() -> None:
    ggc = _static(element_id="GGC", interactable=True)
    gc = _static(element_id="GC", interactable=True, children=[ggc])
    c = _static(element_id="C", interactable=True, children=[gc])
    p = _static(element_id="P", interactable=False, disabled=True, children=[c])
    assert _el(p).find_deepest_interactable_descendant_in_single_chain() == "GGC"


def test_siblings_return_none() -> None:
    a = _static(element_id="A", interactable=True)
    b = _static(element_id="B", interactable=True)
    p = _static(element_id="P", interactable=False, disabled=True, children=[a, b])
    assert _el(p).find_deepest_interactable_descendant_in_single_chain() is None


def test_cousins_return_none() -> None:
    gca = _static(element_id="GCA", interactable=True)
    ca = _static(element_id="CA", interactable=False, children=[gca])
    gcb = _static(element_id="GCB", interactable=True)
    cb = _static(element_id="CB", interactable=False, children=[gcb])
    p = _static(element_id="P", interactable=False, disabled=True, children=[ca, cb])
    assert _el(p).find_deepest_interactable_descendant_in_single_chain() is None


def test_chain_plus_sibling_returns_none() -> None:
    gc = _static(element_id="GC", interactable=True)
    a = _static(element_id="A", interactable=True, children=[gc])
    b = _static(element_id="B", interactable=True)
    p = _static(element_id="P", interactable=False, disabled=True, children=[a, b])
    assert _el(p).find_deepest_interactable_descendant_in_single_chain() is None


def test_disabled_excluded() -> None:
    c = _static(element_id="C", interactable=True, disabled=True)
    p = _static(element_id="P", interactable=False, disabled=True, children=[c])
    assert _el(p).find_deepest_interactable_descendant_in_single_chain() is None


def test_aria_disabled_true_excluded() -> None:
    c = _static(element_id="C", interactable=True, aria_disabled="true")
    p = _static(element_id="P", interactable=False, disabled=True, children=[c])
    assert _el(p).find_deepest_interactable_descendant_in_single_chain() is None


def test_aria_disabled_false_viable() -> None:
    c = _static(element_id="C", interactable=True, aria_disabled="false")
    p = _static(element_id="P", interactable=False, disabled=True, children=[c])
    assert _el(p).find_deepest_interactable_descendant_in_single_chain() == "C"


def test_hover_only_excluded() -> None:
    c = _static(element_id="C", interactable=True, hover_only=True)
    p = _static(element_id="P", interactable=False, disabled=True, children=[c])
    assert _el(p).find_deepest_interactable_descendant_in_single_chain() is None


def test_no_id_excluded() -> None:
    c = _static(element_id="", interactable=True)
    p = _static(element_id="P", interactable=False, disabled=True, children=[c])
    assert _el(p).find_deepest_interactable_descendant_in_single_chain() is None


def test_exotic_disabled_attr_type_not_treated_as_disabled() -> None:
    for exotic in [42, {"x": 1}, [1, 2, 3]]:
        c = _static(element_id="C", interactable=True)
        c["attributes"]["disabled"] = exotic
        p = _static(element_id="P", interactable=False, disabled=True, children=[c])
        assert _el(p).find_deepest_interactable_descendant_in_single_chain() == "C"


def test_row_and_action_in_chain_returns_action() -> None:
    action = _static(element_id="AABO", interactable=True)
    row = _static(element_id="AABK", interactable=True, children=[action])
    wrapper = _static(element_id="AABJ", interactable=False, disabled=True, children=[row])
    assert _el(wrapper).find_deepest_interactable_descendant_in_single_chain() == "AABO"


@pytest.mark.asyncio
async def test_handler_retargets_to_deepest() -> None:
    action_c = _static(element_id="AABO", interactable=True)
    row = _static(element_id="AABK", interactable=True, children=[action_c])
    p = _static(element_id="PARENT", interactable=False, disabled=True, children=[row])
    parent_el = _el(p)
    child_el = _el(action_c)
    child_el.is_disabled = AsyncMock(return_value=False)
    dom_mock = MagicMock()
    dom_mock.safe_get_skyvern_element_by_id = AsyncMock(return_value=child_el)
    action_mock = MagicMock()
    action_mock.element_id = "PARENT"
    from skyvern.webeye.actions.handler import _retarget_disabled_element_for_click

    result = await _retarget_disabled_element_for_click(dom_mock, parent_el, action_mock)
    assert result is child_el
    assert action_mock.element_id == "AABO"


@pytest.mark.asyncio
async def test_handler_child_dynamically_disabled() -> None:
    action_c = _static(element_id="AABO", interactable=True)
    row = _static(element_id="AABK", interactable=True, children=[action_c])
    p = _static(element_id="PARENT", interactable=False, disabled=True, children=[row])
    parent_el = _el(p)
    child_el = _el(action_c)
    child_el.is_disabled = AsyncMock(return_value=True)
    dom_mock = MagicMock()
    dom_mock.safe_get_skyvern_element_by_id = AsyncMock(return_value=child_el)
    action_mock = MagicMock()
    action_mock.element_id = "PARENT"
    from skyvern.webeye.actions.handler import _retarget_disabled_element_for_click

    result = await _retarget_disabled_element_for_click(dom_mock, parent_el, action_mock)
    assert result is None
    assert action_mock.element_id == "PARENT"


@pytest.mark.asyncio
async def test_handler_child_not_found() -> None:
    p = _static(
        element_id="PARENT", interactable=False, disabled=True, children=[_static(element_id="C", interactable=True)]
    )
    parent_el = _el(p)
    dom_mock = MagicMock()
    dom_mock.safe_get_skyvern_element_by_id = AsyncMock(return_value=None)
    action_mock = MagicMock()
    action_mock.element_id = "PARENT"
    from skyvern.webeye.actions.handler import _retarget_disabled_element_for_click

    result = await _retarget_disabled_element_for_click(dom_mock, parent_el, action_mock)
    assert result is None
    assert action_mock.element_id == "PARENT"
