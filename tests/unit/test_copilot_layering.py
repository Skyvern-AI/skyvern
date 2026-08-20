"""Layering guards: the copilot domain package must not depend on the HTTP routes layer."""

import subprocess
import sys


def test_importing_copilot_tools_does_not_load_routes() -> None:
    script = (
        "import sys\n"
        "import skyvern.forge.sdk.copilot.tools\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('skyvern.forge.sdk.routes'))\n"
        "assert not loaded, f'copilot.tools pulled in routes modules: {loaded}'\n"
    )
    # Timeout so a pathological import hang fails loudly instead of stalling the run.
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
