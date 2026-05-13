"""Navigation / download codegen inside while_loop uses same loop body path as for_loop."""

from skyvern.core.script_generations.generate_script import _build_block_fn


def _click() -> dict:
    return {
        "action_type": "click",
        "xpath": "//a",
        "element_id": "e1",
        "reasoning": "go",
    }


def test_navigation_while_body_uses_loop_item_selector() -> None:
    block = {
        "label": "nav",
        "block_type": "navigation",
        "url": "https://example.com",
        "navigation_goal": "Open link",
    }
    fn = _build_block_fn(block, [_click()], is_in_for_loop=True)
    import libcst as cst

    code = cst.Module(body=[fn]).code
    assert "context.loop_item_selector()" in code
