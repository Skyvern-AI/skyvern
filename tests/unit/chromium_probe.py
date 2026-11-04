import asyncio
import functools
from pathlib import Path

from playwright.sync_api import sync_playwright


@functools.cache
def chromium_available() -> bool:
    # Playwright's sync API cannot start on a running event loop; fail loudly rather than cache "not installed".
    # From async code, call this through asyncio.to_thread.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("chromium_available() cannot run on an event loop; use asyncio.to_thread")
    # Launch rather than check executable_path: a headless launch runs the separate headless-shell build, which a
    # host installed with `playwright install chromium --only-shell` has without the full one.
    try:
        with sync_playwright() as playwright:
            try:
                playwright.chromium.launch(headless=True).close()
            except Exception as error:
                # Only a missing executable means not installed. Any other launch failure runs the tests, so they
                # fail with that error instead of skipping silently.
                return "Executable doesn't exist" not in str(error)
    except Exception:
        return False
    return True


@functools.cache
def full_chromium_available() -> bool:
    # A launch with channel="chromium" runs the full build, which a `--only-shell` host lacks even when
    # chromium_available() is True.
    try:
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).exists()
    except Exception:
        return False
