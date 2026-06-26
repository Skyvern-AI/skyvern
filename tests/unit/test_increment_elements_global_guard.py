"""
Regression tests for the getIncrementElements crash guard in domUtils.js.

A mid-click context reset (SPA re-render, iframe document swap, soft nav) can
wipe window.globalParsedElementCounter while the top-frame URL guard still
passes, so getIncrementElements used to dereference `.get()` on undefined and
throw a TypeError. The behavioral Node test proves the wait loop now skips when
the counter is gone, and the source check pins the top-level guard initializer.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_DOMUTILS = _REPO_ROOT / "skyvern" / "webeye" / "scraper" / "domUtils.js"
_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
class TestIncrementElementsGlobalGuard:
    def test_js_syntax(self):
        result = subprocess.run(
            [_NODE, "--check", str(_DOMUTILS)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"JS syntax error: {result.stderr}"

    def test_behavioral(self):
        script = Path(__file__).parent / "test_increment_elements_global_guard.js"
        assert script.exists(), f"Missing {script}"
        result = subprocess.run(
            [_NODE, str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"Failed:\n{result.stdout}\n{result.stderr}"

    def test_top_level_guard_initializer_present(self):
        source = _DOMUTILS.read_text()
        assert re.search(
            r"if\s*\(\s*window\.globalParsedElementCounter\s*===\s*undefined\s*\)",
            source,
        ), "globalParsedElementCounter must be guard-initialized at top level like its sibling globals"
