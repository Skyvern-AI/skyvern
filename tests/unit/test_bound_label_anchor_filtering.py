"""Slice 1 (SKY-12543): the bound-label fallbacks must not return a ``<label>``
whose click would be stolen by a nested interactive descendant (an ``<a href>``
or ``<button>``).

HTML label semantics forward a label click to the bound control only when the
pointer does not land on interactive content nested inside the label.  A custom
checkout checkbox whose visually-hidden ``<input>`` is bound to a label that
wraps the terms-and-conditions ``<a href>`` therefore navigates to the terms
page instead of toggling — the loop-to-max-steps failure in SKY-12543.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.utils.dom import SkyvernElement


def _make_descendant_locator(interactive_count: int) -> MagicMock:
    descendant = MagicMock()
    descendant.count = AsyncMock(return_value=interactive_count)
    return descendant


def _make_raising_descendant_locator(error: Exception) -> MagicMock:
    descendant = MagicMock()
    descendant.count = AsyncMock(side_effect=error)
    return descendant


def _make_label_locator(interactive_count: int) -> MagicMock:
    label = MagicMock()
    label.count = AsyncMock(return_value=1)
    label.locator = MagicMock(return_value=_make_descendant_locator(interactive_count))
    return label


def _make_input_for_attr_id(label_locator: MagicMock, element_id: str = "accept_tos") -> SkyvernElement:
    elem = object.__new__(SkyvernElement)
    elem.get_tag_name = MagicMock(return_value="input")  # type: ignore[method-assign]
    elem.get_attr = AsyncMock(return_value=element_id)  # type: ignore[method-assign]
    frame = MagicMock()
    frame.locator = MagicMock(return_value=label_locator)
    elem.get_frame = MagicMock(return_value=frame)  # type: ignore[method-assign]
    return elem


def _make_parent_label_locator(interactive_count: int, tag: str = "LABEL") -> MagicMock:
    parent = MagicMock()
    parent.count = AsyncMock(return_value=1)
    parent.evaluate = AsyncMock(return_value=tag)
    parent.locator = MagicMock(return_value=_make_descendant_locator(interactive_count))
    return parent


def _make_input_for_direct_parent(parent_locator: MagicMock) -> SkyvernElement:
    elem = object.__new__(SkyvernElement)
    elem.get_tag_name = MagicMock(return_value="input")  # type: ignore[method-assign]
    self_locator = MagicMock()
    self_locator.locator = MagicMock(return_value=parent_locator)
    elem.get_locator = MagicMock(return_value=self_locator)  # type: ignore[method-assign]
    return elem


class TestFindBoundLabelByAttrIdAnchorFiltering:
    @pytest.mark.asyncio
    async def test_label_wrapping_anchor_href_is_rejected(self) -> None:
        elem = _make_input_for_attr_id(_make_label_locator(interactive_count=1))
        assert await elem.find_bound_label_by_attr_id() is None

    @pytest.mark.asyncio
    async def test_ordinary_label_without_interactive_descendant_is_usable(self) -> None:
        label = _make_label_locator(interactive_count=0)
        elem = _make_input_for_attr_id(label)
        assert await elem.find_bound_label_by_attr_id() is label

    @pytest.mark.asyncio
    async def test_guard_selector_covers_anchor_and_button(self) -> None:
        label = _make_label_locator(interactive_count=0)
        elem = _make_input_for_attr_id(label)
        await elem.find_bound_label_by_attr_id()
        selector = label.locator.call_args.args[0]
        assert "a[href]" in selector
        assert "button" in selector

    @pytest.mark.asyncio
    async def test_no_matching_label_skips_descendant_check(self) -> None:
        label = MagicMock()
        label.count = AsyncMock(return_value=0)
        label.locator = MagicMock()
        elem = _make_input_for_attr_id(label)
        assert await elem.find_bound_label_by_attr_id() is None
        label.locator.assert_not_called()


class TestFindBoundLabelByDirectParentAnchorFiltering:
    @pytest.mark.asyncio
    async def test_wrapping_label_with_anchor_is_rejected(self) -> None:
        elem = _make_input_for_direct_parent(_make_parent_label_locator(interactive_count=1))
        assert await elem.find_bound_label_by_direct_parent() is None

    @pytest.mark.asyncio
    async def test_wrapping_label_with_only_checkbox_is_usable(self) -> None:
        parent = _make_parent_label_locator(interactive_count=0)
        elem = _make_input_for_direct_parent(parent)
        assert await elem.find_bound_label_by_direct_parent() is parent

    @pytest.mark.asyncio
    async def test_non_label_parent_returns_none(self) -> None:
        elem = _make_input_for_direct_parent(_make_parent_label_locator(interactive_count=0, tag="DIV"))
        assert await elem.find_bound_label_by_direct_parent() is None


class TestLabelClickForwardsToDescendantProbeFallback:
    """The descendant probe can raise on a destroyed context or detached frame. The
    fallback is caller-selected: OSS bound-label callers keep the pre-PR fail-open
    default, while the Cloud setup path opts into fail-closed."""

    @pytest.mark.asyncio
    async def test_probe_failure_defaults_to_not_forwarding(self) -> None:
        label = MagicMock()
        label.locator = MagicMock(return_value=_make_raising_descendant_locator(RuntimeError("detached frame")))
        assert await SkyvernElement._label_click_forwards_to_descendant(label) is False

    @pytest.mark.asyncio
    async def test_probe_failure_fails_closed_when_requested(self) -> None:
        label = MagicMock()
        label.locator = MagicMock(return_value=_make_raising_descendant_locator(RuntimeError("detached frame")))
        assert await SkyvernElement._label_click_forwards_to_descendant(label, fail_closed=True) is True

    @pytest.mark.asyncio
    async def test_no_descendant_is_not_forwarding(self) -> None:
        label = MagicMock()
        label.locator = MagicMock(return_value=_make_descendant_locator(0))
        assert await SkyvernElement._label_click_forwards_to_descendant(label) is False

    @pytest.mark.asyncio
    async def test_attr_id_fallback_keeps_label_when_probe_raises(self) -> None:
        label = MagicMock()
        label.count = AsyncMock(return_value=1)
        label.locator = MagicMock(return_value=_make_raising_descendant_locator(RuntimeError("destroyed context")))
        elem = _make_input_for_attr_id(label)
        assert await elem.find_bound_label_by_attr_id() is label

    @pytest.mark.asyncio
    async def test_direct_parent_fallback_keeps_label_when_probe_raises(self) -> None:
        parent = MagicMock()
        parent.count = AsyncMock(return_value=1)
        parent.evaluate = AsyncMock(return_value="LABEL")
        parent.locator = MagicMock(return_value=_make_raising_descendant_locator(RuntimeError("destroyed context")))
        elem = _make_input_for_direct_parent(parent)
        assert await elem.find_bound_label_by_direct_parent() is parent
