"""
Element-tree scraper crash-guard regression tests.

Runs a behavioral Node.js test that exercises the two domUtils.js crash paths:
undefined `className` in isHoverPointerElement, and the isElementVisible
recursion cycle on a display:contents element holding a checkbox/radio/option.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_DOMUTILS = _REPO_ROOT / "skyvern" / "webeye" / "scraper" / "domUtils.js"
_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
class TestDomScrapeCrashGuards:
    def test_js_syntax(self):
        result = subprocess.run(
            [_NODE, "--check", str(_DOMUTILS)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"JS syntax error: {result.stderr}"

    def test_behavioral(self):
        script = Path(__file__).parent / "test_dom_scrape_crash_guards.js"
        assert script.exists(), f"Missing {script}"
        result = subprocess.run(
            [_NODE, str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"Failed:\n{result.stdout}\n{result.stderr}"

    def test_datepicker_navigation_behavioral(self):
        script = Path(__file__).parent / "test_datepicker_navigation_domutils.js"
        assert script.exists(), f"Missing {script}"
        result = subprocess.run(
            [_NODE, str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"Failed:\n{result.stdout}\n{result.stderr}"

    def test_injection_scope_isolation(self, tmp_path):
        from skyvern.webeye.utils.page import load_js_script

        script = Path(__file__).parent / "test_domutils_injection_scope.js"
        assert script.exists(), f"Missing {script}"
        loaded_path = tmp_path / "loaded_domutils.js"
        loaded_path.write_text(load_js_script(), encoding="utf-8")
        result = subprocess.run(
            [_NODE, str(script), str(_DOMUTILS), str(loaded_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"Failed:\n{result.stdout}\n{result.stderr}"
