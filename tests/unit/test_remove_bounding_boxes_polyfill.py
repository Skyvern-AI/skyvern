"""
removeBoundingBoxes() must survive a Prototype.js-style monkey-patch of
Element.prototype.remove.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
class TestRemoveBoundingBoxesPolyfill:
    def test_behavioral(self):
        script = Path(__file__).parent / "test_remove_bounding_boxes_polyfill.js"
        assert script.exists(), f"Missing {script}"
        result = subprocess.run(
            [_NODE, str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"Failed:\n{result.stdout}\n{result.stderr}"
