"""Persist/restore session-only cookies that Chromium drops from a profile's user_data_dir snapshot."""

import contextlib
import json
import os

import structlog
from playwright.async_api import BrowserContext

from skyvern.webeye.profile_cookie_merge import (
    _ALLOWED_COOKIE_KEYS,
    BANKED_COOKIES_FILENAME,
    clear_banked_cookies,
    union_cookies_into_profile_dir,
)

LOG = structlog.get_logger()

SESSION_COOKIES_FILENAME = ".skyvern_session_cookies.json"

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


def read_persisted_session_cookies(user_data_dir: str | None) -> list[dict]:
    """Read the end-state session cookies that ``close()`` persisted to the sidecar, for callers that
    need them after the live context is gone (the delta-merge freshness guard on a completed run whose
    browser was already closed). Only session cookies are captured here — persistent cookies live in the
    encrypted Chromium store — but those are the login-relevant ones the guard protects."""
    if not user_data_dir:
        return []
    path = os.path.join(user_data_dir, SESSION_COOKIES_FILENAME)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            cookies = json.load(f)
    except (OSError, ValueError):
        return []
    return [{k: v for k, v in c.items() if k in _ALLOWED_COOKIE_KEYS} for c in cookies if isinstance(c, dict)]


async def _add_cookies_with_fallback(browser_context: BrowserContext, cookies: list[dict], kind: str) -> None:
    """add_cookies as one batch, falling back to per-cookie so a single bad cookie can't drop the rest."""
    try:
        await browser_context.add_cookies(cookies)
        LOG.info("Restored cookies into browser profile", kind=kind, cookie_count=len(cookies), sampling=True)
    except Exception:
        restored = 0
        for cookie in cookies:
            try:
                await browser_context.add_cookies([cookie])
                restored += 1
            except Exception:
                LOG.debug("Skipped a cookie during restore", kind=kind, name=cookie.get("name"), exc_info=True)
                continue
        LOG.warning(
            "Batch cookie restore failed; restored individually",
            kind=kind,
            restored=restored,
            failed=len(cookies) - restored,
            total=len(cookies),
        )


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
        await _add_cookies_with_fallback(browser_context, sanitized, "session")
    except Exception:
        LOG.warning("Failed to restore session cookies", exc_info=True)


async def restore_banked_cookies(browser_context: BrowserContext | None, user_data_dir: str | None) -> None:
    """Re-inject cookies unioned by ``profile_cookie_merge.union_cookies_into_profile_dir``.

    Called AFTER ``restore_session_cookies`` so a verified-login heal wins over the profile's own
    older session sidecar on a (domain, name, path) clash. Persistent expiries are preserved;
    session expiries (-1/0) are pinned to -1 for the same patchright reason as restore_session_cookies.
    """
    try:
        if browser_context is None or not user_data_dir or not os.path.isdir(user_data_dir):
            return
        path = os.path.join(user_data_dir, BANKED_COOKIES_FILENAME)
        if not os.path.exists(path):
            return
        with open(path) as f:
            cookies = json.load(f)
        sanitized = [
            {
                **{k: v for k, v in cookie.items() if k in _ALLOWED_COOKIE_KEYS},
                **({"expires": -1} if cookie.get("expires", -1) in _SESSION_COOKIE_EXPIRES else {}),
            }
            for cookie in cookies
            if cookie.get("name") and cookie.get("domain")
        ]
        if not sanitized:
            return
        await _add_cookies_with_fallback(browser_context, sanitized, "banked")
    except Exception:
        LOG.warning("Failed to restore banked cookies", exc_info=True)


async def refresh_banked_cookies(browser_context: BrowserContext | None, user_data_dir: str | None) -> None:
    """Replace the banked-cookies sidecar in ``user_data_dir`` with the live context's cookie jar.

    Chromium commits its Cookies database on a ~30s timer, so a directory archived from a running browser
    can hold a database that predates the last sign-in and the sidecar carries what it is missing. Without
    a live context the sidecar is only dropped: the browser has flushed its database, and a sidecar
    inherited from the seed archive would otherwise replay stale cookies over fresher state at every boot.
    """
    if not user_data_dir or not os.path.isdir(user_data_dir):
        return
    cookies: list[dict] = []
    if browser_context is not None:
        try:
            cookies = await browser_context.cookies()
        except Exception as exc:
            LOG.info("Live cookie jar unavailable; dropping the banked cookies", error_type=type(exc).__name__)
    clear_banked_cookies(user_data_dir)
    if cookies:
        union_cookies_into_profile_dir(cookies, user_data_dir)
