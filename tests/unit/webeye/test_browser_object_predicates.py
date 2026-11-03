from __future__ import annotations

from unittest.mock import MagicMock

from playwright.async_api import Frame, Page

from skyvern.webeye.browser_object_predicates import is_page_like


def test_matches_isinstance_page_across_representative_objects() -> None:
    # The predicate must be a drop-in for isinstance(obj, Page) at the one
    # migrated call site: identical verdict for a Page, a Frame, and their
    # spec'd mocks (which back the existing suite).
    for obj in (MagicMock(spec=Page), MagicMock(spec=Frame), object(), "not-a-page"):
        assert is_page_like(obj) == isinstance(obj, Page)


def test_page_like_rejects_object_carrying_frame_ownership_markers() -> None:
    # Even a duck object that exposes every page capability is rejected when it
    # also reports a parent page/frame -- that is a subframe, not a page.
    frame_shaped = MagicMock(spec=["main_frame", "context", "bring_to_front", "evaluate", "page", "parent_frame"])
    assert is_page_like(frame_shaped) is False


def test_engine_neutral_page_from_a_distinct_class_is_page_like() -> None:
    # A second engine's Page has a distinct class identity, so isinstance against
    # a concrete driver's Page would return False for it; the structural
    # predicate accepts it because it genuinely presents the full PageLike
    # capability surface (main_frame, context, bring_to_front, evaluate).
    class OtherEnginePage:
        main_frame = object()
        context = object()

        def bring_to_front(self) -> None: ...

        def evaluate(self, expression: str, arg: object = None) -> None: ...

    assert isinstance(OtherEnginePage(), Page) is False
    assert is_page_like(OtherEnginePage()) is True
