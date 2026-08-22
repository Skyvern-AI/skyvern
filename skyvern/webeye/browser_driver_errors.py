"""Driver-native error identities for callers with no pinned ``BrowserEngineSelection``."""

from __future__ import annotations

import importlib

from playwright.async_api import Error as _ImportedDriverError
from playwright.async_api import TimeoutError as _ImportedDriverTimeoutError

# Two Playwright-family driver packages can be live in one process: scripts/patch_browser.sh repoints
# this file's driver import to the fork across the browser image but deliberately skips
# cloud/persistent_browsers, so a persistent session's pages raise the un-rewritten package's error
# classes. Package names are spelled as bare strings, and the import prefix is never written
# literally anywhere in this file, because that rewrite matches the literal prefix and would repoint
# the scan along with the import.
_DRIVER_PACKAGES = ("playwright", "patchright")


def _load_driver_error_types() -> tuple[tuple[type[BaseException], ...], tuple[type[BaseException], ...]]:
    # Seeded from the static import so the result is never empty and always carries whichever package
    # the build repointed this file at; the scan only ever adds the other one.
    error_types: list[type[BaseException]] = [_ImportedDriverError]
    timeout_types: list[type[BaseException]] = [_ImportedDriverTimeoutError]
    for package in _DRIVER_PACKAGES:
        try:
            api = importlib.import_module(f"{package}.async_api")
            driver_error, driver_timeout_error = api.Error, api.TimeoutError
        except (ImportError, AttributeError):
            continue
        if driver_error not in error_types:
            error_types.append(driver_error)
        if driver_timeout_error not in timeout_types:
            timeout_types.append(driver_timeout_error)
    return tuple(error_types), tuple(timeout_types)


DRIVER_ERROR_TYPES, DRIVER_TIMEOUT_ERROR_TYPES = _load_driver_error_types()


def is_driver_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a driver-family error from any Playwright-family package installed here."""
    return isinstance(exc, DRIVER_ERROR_TYPES)


def is_driver_timeout_error(exc: BaseException) -> bool:
    return isinstance(exc, DRIVER_TIMEOUT_ERROR_TYPES)
