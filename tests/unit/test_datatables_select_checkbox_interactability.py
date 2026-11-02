"""
DataTables Select per-row checkbox cell interactability.

Runs behavioral Node.js tests that exercise isInteractable against a mock DOM.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
class TestDataTablesSelectCheckboxInteractability:
    def test_behavioral(self):
        script = Path(__file__).parent / "test_datatables_select_checkbox_interactability.js"
        assert script.exists(), f"Missing {script}"
        result = subprocess.run(
            [_NODE, str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"Failed:\n{result.stdout}\n{result.stderr}"
