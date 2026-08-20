"""Guard: no new test files inside the shipped `skyvern` package.

`skyvern/` is synced to the open-source repo and shipped as an installable
package. Test files belong under `tests/`, never inside the package — an
in-package test ships to every install and can drag test-only imports
(`pytest`, `pytest_asyncio`, ...) into the runtime dependency graph.

pytest's default `python_files` collects BOTH naming conventions
(`test_*.py` and `*_test.py`), and this repo sets no override, so the guard
checks both. Two legacy `*_test.py` modules predate the guard and are
allowlisted as tracked debt — SKY-12351 relocates them and empties the list.
"""

from __future__ import annotations

from pathlib import Path

import skyvern

# Pre-existing in-package test modules, tracked for relocation by SKY-12351.
# Do NOT add to this list — new test files belong under tests/, not in skyvern/.
_KNOWN_LEGACY = {
    "forge/sdk/db/agent_db_test.py",
    "forge/sdk/api/llm/utils_test.py",
}


def _in_package_test_files() -> set[str]:
    package_root = Path(skyvern.__file__).parent
    matches = set(package_root.rglob("test_*.py")) | set(package_root.rglob("*_test.py"))
    # as_posix() so the forward-slash allowlist matches on Windows too.
    return {p.relative_to(package_root).as_posix() for p in matches}


def test_no_new_test_files_in_shipped_package() -> None:
    offenders = sorted(_in_package_test_files() - _KNOWN_LEGACY)
    assert offenders == [], (
        "test files (test_*.py / *_test.py) must live under tests/, not the shipped "
        f"skyvern package; found: {offenders}"
    )
