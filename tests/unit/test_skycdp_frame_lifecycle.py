from __future__ import annotations

from typing import Any

import pytest

from skyvern.webeye.skycdp.facade.page import Page


@pytest.mark.parametrize("parent_arrival", ["navigation", "frame_tree"])
def test_an_orphaned_oopif_is_linked_when_its_parent_arrives(parent_arrival: str) -> None:
    page = Page(object(), object())  # type: ignore[arg-type]
    page._on_frame_navigated({"frame": {"id": "main", "url": "https://top.example"}})
    page._on_frame_attached({"frameId": "deep", "parentFrameId": "middle"})

    deep = page._frames["deep"]
    assert deep.parent_frame is None

    if parent_arrival == "navigation":
        page._on_frame_navigated({"frame": {"id": "middle", "parentId": "main", "url": "https://middle.example"}})
    else:
        session: Any = object()
        page._absorb_frame_tree(
            {
                "frame": {"id": "middle", "parentId": "main", "url": "https://middle.example"},
                "childFrames": [],
            },
            parent=page.main_frame,
            session=session,
        )

    assert deep.parent_frame is page._frames["middle"]
