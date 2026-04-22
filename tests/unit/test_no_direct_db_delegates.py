"""
Verify the no-direct-db-delegates hook script stays consistent.
"""

import subprocess


def test_hook_passes_on_current_codebase() -> None:
    """The hook should pass cleanly — all legacy files are in the allowlist."""
    result = subprocess.run(
        ["./scripts/check_no_direct_db_delegates.sh"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Hook unexpectedly failed. New direct delegate calls found:\n{result.stdout}"
