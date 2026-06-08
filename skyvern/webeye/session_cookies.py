"""Persist/restore session-only cookies that Chromium drops from a profile's user_data_dir snapshot."""

import contextlib
import json
import os

import structlog
from playwright.async_api import BrowserContext

LOG = structlog.get_logger()

SESSION_COOKIES_FILENAME = ".skyvern_session_cookies.json"

# Keys accepted by Playwright's add_cookies; drop anything else (e.g. partitionKey)
# so one unexpected field can't reject the whole batch.
_ALLOWED_COOKIE_KEYS = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}

# A session cookie reports expires -1 (Playwright) or 0 (patchright/stealth-chromium fork); persistent
# cookies carry a real future timestamp and re-hydrate from the snapshot on their own.
_SESSION_COOKIE_EXPIRES = (-1, 0)


async def persist_session_cookies(browser_context: BrowserContext | None, user_data_dir: str | None) -> None:
    """Snapshot the live context's session cookies into a sidecar inside ``user_data_dir``."""
    try:
        if browser_context is None or not user_data_dir or not os.path.isdir(user_data_dir):
            return
        path = os.path.join(user_data_dir, SESSION_COOKIES_FILENAME)
        # A cookie with no "expires" key defaults to -1 here: unknown expiry, treat as a session cookie.
        cookies = [
            cookie for cookie in await browser_context.cookies() if cookie.get("expires", -1) in _SESSION_COOKIE_EXPIRES
        ]
        if not cookies:
            # Drop a stale sidecar from a prior save so a dead session isn't re-injected on the next reuse.
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
            return
        # 0o600: the sidecar holds auth cookies. Write to a temp file and atomically replace so a failed
        # write can't leave a partial file or destroy the previous good sidecar.
        tmp = f"{path}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(cookies, f)
            os.replace(tmp, path)
        finally:
            # os.replace consumes tmp on success; clean it up if the write or replace failed partway.
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp)
        LOG.info("Persisted session cookies for browser profile", cookie_count=len(cookies))
    except Exception:
        LOG.warning("Failed to persist session cookies", exc_info=True)


async def restore_session_cookies(browser_context: BrowserContext | None, user_data_dir: str | None) -> None:
    """Re-inject session cookies captured by ``persist_session_cookies`` if a sidecar exists."""
    try:
        if browser_context is None or not user_data_dir or not os.path.isdir(user_data_dir):
            return
        path = os.path.join(user_data_dir, SESSION_COOKIES_FILENAME)
        if not os.path.exists(path):
            return
        with open(path) as f:
            cookies = json.load(f)
        # Re-filter (old sidecars hold the full cookie set) and pin expires to -1: patchright reports
        # session cookies as expires 0, which add_cookies reads as the Unix epoch (expired) and drops.
        sanitized = [
            {**{k: v for k, v in cookie.items() if k in _ALLOWED_COOKIE_KEYS}, "expires": -1}
            for cookie in cookies
            if cookie.get("expires", -1) in _SESSION_COOKIE_EXPIRES
        ]
        if not sanitized:
            return
        try:
            await browser_context.add_cookies(sanitized)
            LOG.info("Restored session cookies into browser profile", cookie_count=len(sanitized))
        except Exception:
            restored = 0
            for cookie in sanitized:
                try:
                    await browser_context.add_cookies([cookie])
                    restored += 1
                except Exception:
                    LOG.debug("Skipped a cookie during restore", name=cookie.get("name"), exc_info=True)
                    continue
            LOG.warning(
                "Batch cookie restore failed; restored individually",
                restored=restored,
                failed=len(sanitized) - restored,
                total=len(sanitized),
            )
    except Exception:
        LOG.warning("Failed to restore session cookies", exc_info=True)
