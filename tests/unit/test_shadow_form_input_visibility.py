"""
Tests for shadow DOM form input visibility fix.

Web Component libraries often hide native form inputs inside shadow DOM
with CSS while rendering a styled overlay. The scraper must still include
these native inputs so the agent can interact with them.
"""

import subprocess


def _load_js() -> str:
    from skyvern.webeye.scraper.scraper import load_js_script

    return load_js_script()


class TestShadowFormInputVisibility:
    def test_js_syntax_valid(self):
        result = subprocess.run(
            ["node", "--check", "skyvern/webeye/scraper/domUtils.js"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"JS syntax error: {result.stderr}"

    def test_shadow_form_input_guard_exists(self):
        js = _load_js()
        idx_fn = js.index("function isElementVisible(")
        idx_fn_end = js.index("\nfunction ", idx_fn + 1)
        body = js[idx_fn:idx_fn_end]
        assert "getRootNode() instanceof ShadowRoot" in body
        assert "tagLower" in body or "tagName" in body

    def test_guard_covers_input_textarea_select(self):
        js = _load_js()
        idx_fn = js.index("function isElementVisible(")
        idx_fn_end = js.index("\nfunction ", idx_fn + 1)
        body = js[idx_fn:idx_fn_end]
        for tag in ["input", "textarea", "select"]:
            assert f'"{tag}"' in body, f"Shadow DOM guard must cover {tag} elements"

    def test_guard_excludes_hidden_inputs(self):
        js = _load_js()
        idx_fn = js.index("function isElementVisible(")
        idx_guard = js.index("getRootNode() instanceof ShadowRoot", idx_fn)
        guard_block = js[idx_guard : idx_guard + 300]
        assert "hidden" in guard_block, "Guard must exclude input[type=hidden]"

    def test_guard_placed_before_visibility_checks(self):
        js = _load_js()
        idx_fn = js.index("function isElementVisible(")
        idx_guard = js.index("getRootNode() instanceof ShadowRoot", idx_fn)
        idx_rect = js.index("getBoundingClientRect", idx_fn)
        idx_display = js.index('display === "contents"', idx_fn)
        assert idx_guard < idx_rect, "Shadow form guard must run before bounding rect check"
        assert idx_guard < idx_display, "Shadow form guard must run before display:contents check"
