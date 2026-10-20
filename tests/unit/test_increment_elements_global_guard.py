"""Incremental DOM observer source-guard and rolling-deploy getter compatibility tests.

Node/source/getter checks that run without a browser. The real-Chromium observer lifecycle tests
live in tests/browser_e2e/test_taskv3_incremental_observer_memory_e2e.py.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.async_api import Page

from skyvern.webeye.scraper.scraper import IncrementalScrapePage
from skyvern.webeye.utils.page import SkyvernFrame

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("window_state", "expected_count"),
        [
            (
                {
                    "globalObserverForDOMIncrement": {"skyvernObserverVersion": 1},
                    "INCREMENTAL_OBSERVER_VERSION": 1,
                    "globalIncrementalJobCount": 7,
                },
                7,
            ),
            (
                {
                    "globalObserverForDOMIncrement": {"skyvernObserverVersion": 1},
                    "INCREMENTAL_OBSERVER_VERSION": 1,
                    "globalIncrementalJobCount": 7,
                    "globalOneTimeIncrementElements": [{}, {}],
                },
                7,
            ),
            (
                {
                    "globalObserverForDOMIncrement": {"skyvernObserverVersion": 1},
                    "INCREMENTAL_OBSERVER_VERSION": 1,
                    "globalIncrementalJobCount": 0,
                    "globalOneTimeIncrementElements": [{}, {}],
                },
                0,
            ),
            (
                {
                    "globalObserverForDOMIncrement": {},
                    "INCREMENTAL_OBSERVER_VERSION": 1,
                    "globalIncrementalJobCount": 5,
                    "globalOneTimeIncrementElements": [{}],
                },
                5,
            ),
            (
                {
                    "globalObserverForDOMIncrement": {"skyvernObserverVersion": 999},
                    "INCREMENTAL_OBSERVER_VERSION": 1,
                    "globalIncrementalJobCount": 5,
                    "globalOneTimeIncrementElements": [{}],
                },
                5,
            ),
            (
                {
                    "globalObserverForDOMIncrement": {"skyvernObserverVersion": 999},
                    "INCREMENTAL_OBSERVER_VERSION": 1,
                    "globalIncrementalJobCount": 0,
                    "globalOneTimeIncrementElements": [{}, {}],
                },
                2,
            ),
            (
                {
                    "globalObserverForDOMIncrement": {},
                    "globalIncrementalJobCount": 0,
                    "globalOneTimeIncrementElements": [{}, {}],
                },
                2,
            ),
            ({"globalObserverForDOMIncrement": {}, "globalOneTimeIncrementElements": [{}, {}]}, 2),
            ({"globalObserverForDOMIncrement": {}, "globalOneTimeIncrementElements": []}, 0),
            ({}, 0),
        ],
        ids=[
            "current-scalar",
            "current-scalar-wins",
            "current-scalar-zero",
            "unstamped-splicing-scalar-wins",
            "version-mismatch-splicing-scalar-wins",
            "version-mismatch-scalar-zero-array-wins",
            "legacy-scalar-zero-array-wins",
            "legacy-array",
            "legacy-empty",
            "navigation",
        ],
    )
    async def test_incremental_elements_num_rolling_deploy_compatibility(
        self, window_state: dict[str, object], expected_count: int
    ) -> None:
        def evaluate(expression: str, arg: object = None) -> int | bool:
            # Execute the getter's JavaScript; a canned evaluate result would miss compatibility regressions.
            result = subprocess.run(
                [
                    _NODE,
                    "-e",
                    (
                        "const vm = require('node:vm');"
                        "const window = JSON.parse(process.argv[1]);"
                        "const read = vm.runInNewContext(process.argv[2], {window});"
                        "process.stdout.write(JSON.stringify(read()));"
                    ),
                    json.dumps(window_state),
                    expression,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            return json.loads(result.stdout)

        page = MagicMock(spec=Page, context=None)
        page.evaluate.side_effect = evaluate
        scraped = IncrementalScrapePage(SkyvernFrame(page))
        assert await scraped.get_incremental_elements_num() == expected_count
