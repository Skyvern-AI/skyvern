"""Whether an optional E2E dependency is present, and whether its absence may skip.

The compatibility suite needs things a bare unit-test runner has no reason to carry:
a real Chromium, node, the pinned JS clients. Skipping when they are missing keeps the
suite usable on a laptop; skipping SILENTLY in CI turns the merge gate into decoration.

So CI sets CDP_PROXY_E2E_REQUIRED=1 and every one of those skips becomes a failure.
The suite is a gate only on a runner that actually has to run it.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

E2E_REQUIRED_ENV = "CDP_PROXY_E2E_REQUIRED"

JS_CLIENT_DIR = Path(__file__).parent / "fixtures" / "js_clients"


def e2e_required() -> bool:
    return os.environ.get(E2E_REQUIRED_ENV) == "1"


def require(available: bool, reason: str) -> None:
    """Skip when a dependency is missing — unless CI declared it mandatory."""
    if available:
        return
    if e2e_required():
        pytest.fail(f"{reason} — required because {E2E_REQUIRED_ENV}=1")
    pytest.skip(reason)


@lru_cache(maxsize=1)
def find_node() -> str | None:
    return shutil.which("node")


@lru_cache(maxsize=1)
def playwright_python_available() -> bool:
    return importlib.util.find_spec("playwright.async_api") is not None


@lru_cache(maxsize=1)
def js_clients_installed() -> bool:
    """The pinned clients are vendored by `npm ci` in fixtures/js_clients."""
    return (JS_CLIENT_DIR / "node_modules").is_dir()


def run_js_client(script: str, endpoint: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """Drives one JS CDP client against `endpoint`. Bounded: a hung client fails the
    test rather than hanging the job."""
    return subprocess.run(
        [find_node() or "node", script, endpoint],
        cwd=JS_CLIENT_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
