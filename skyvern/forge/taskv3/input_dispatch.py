"""The one place Task V3 sends pointer and keyboard input to a page.

Each gesture runs the Playwright call each tool made before this module existed, argument for argument. The
TASK_V3_LOGIN_PACE treatment holds the first click or Enter after a password fill until LOGIN_PACE_FLOOR_S after Task V3
started the block.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeAlias, cast

import structlog
from playwright.async_api import ElementHandle, FileChooser, Frame, Locator, Page

from skyvern.config import settings
from skyvern.forge.taskv3.run_arms import LOGIN_PACE_FLAG, run_arm_enabled
from skyvern.webeye.browser_object_predicates import is_page_like

LOG = structlog.get_logger()

Realm: TypeAlias = Page | Frame
Target: TypeAlias = str | Locator | ElementHandle

LOGIN_PACE_FLOOR_S = 45.0
_CANCEL_POLL_S = 2.0


class LoginPaceRefused(RuntimeError):
    """The task was canceled, or the page moved, during the wait, so the held gesture is not sent."""


@dataclass(frozen=True)
class SecretFillAnchor:
    top_url: str | None
    frame: Frame | None
    frame_url: str | None


@dataclass
class _LoginPace:
    origin: float
    should_cancel: Callable[[], Awaitable[bool]] | None = None
    anchor: SecretFillAnchor | None = None
    navigated: bool = False
    unsubscribe: Callable[[], None] | None = None
    done: bool = False
    canceled: bool = False


_login_pace: ContextVar[_LoginPace | None] = ContextVar("taskv3_login_pace", default=None)


def top_url(realm: Realm) -> str | None:
    try:
        return cast(Page, realm if is_page_like(realm) else cast(Frame, realm).page).url
    except Exception:
        return None


def start_login_pace(should_cancel: Callable[[], Awaitable[bool]] | None = None) -> None:
    """Start the block's clock at Task V3 start, after the block's navigation has landed (about 10 s after the block
    is created). Later pages of the same login run on the same clock."""
    on = run_arm_enabled(LOGIN_PACE_FLAG, settings.TASK_V3_LOGIN_PACE)
    _login_pace.set(_LoginPace(origin=time.monotonic(), should_cancel=should_cancel) if on else None)


def secret_fill_anchor(realm: Realm) -> SecretFillAnchor:
    """Read before the fill, so a fill that moves the page or the frame does not move the anchor."""
    frame = None if is_page_like(realm) else cast(Frame, realm)
    return SecretFillAnchor(top_url(realm), frame, frame.url if frame is not None else None)


def _unsubscribe(pace: _LoginPace) -> None:
    if pace.unsubscribe is not None:
        pace.unsubscribe()
        pace.unsubscribe = None


def note_secret_fill(realm: Realm, anchor: SecretFillAnchor) -> None:
    pace = _login_pace.get()
    if pace is None or pace.done:
        return
    _unsubscribe(pace)
    pace.anchor, pace.navigated = anchor, False
    try:
        page = cast(Page, realm if is_page_like(realm) else cast(Frame, realm).page)

        # A same-URL reload keeps both URLs, so only the event shows it; a same-URL history write also fires it.
        def _on_navigated(frame: Frame) -> None:
            if frame is anchor.frame or frame.parent_frame is None:
                pace.navigated = True

        page.on("framenavigated", _on_navigated)
        pace.unsubscribe = lambda: page.remove_listener("framenavigated", _on_navigated)
    except Exception:
        LOG.warning("Task V3 login pace could not watch for a reload", exc_info=True)


def _moved_by(pace: _LoginPace, anchor: SecretFillAnchor, realm: Realm) -> str | None:
    """Which check saw the password's document move, or None. An unreadable URL counts as moved."""
    url = top_url(realm)
    if url is None or url != anchor.top_url:
        return "top_url"
    if anchor.frame is not None:
        try:
            if anchor.frame.is_detached():
                return "frame_detached"
            if anchor.frame.url != anchor.frame_url:
                return "frame_url"
        except Exception:
            return "frame_unreadable"
    return "navigated_event" if pace.navigated else None


def _not_held(pace: _LoginPace, reason: str, moved_by: str | None = None) -> None:
    pace.done = True
    _unsubscribe(pace)
    LOG.info(
        "Task V3 login pace submit not held",
        reason=reason,
        nav_s=round(time.monotonic() - pace.origin, 1),
        **({"moved_by": moved_by} if moved_by else {}),
    )


async def _cancel_seen(pace: _LoginPace) -> bool:
    try:
        return pace.should_cancel is not None and await pace.should_cancel()
    except Exception:
        # A failed status read must not refuse a healthy submit; the loop's own poll still catches the cancel.
        return False


async def _hold_submit(realm: Realm) -> None:
    pace = _login_pace.get()
    if pace is not None and pace.canceled:
        # Sticky: a caller that swallows the refusal and sends another gesture must not get the credentials out.
        raise LoginPaceRefused("the task was canceled during the login wait; the gesture was not sent")
    if pace is None or pace.done or (anchor := pace.anchor) is None:
        return
    if moved_by := _moved_by(pace, anchor, realm):
        # The page left the password page before any click or Enter reached here: something else submitted.
        _not_held(pace, "page_moved_before_submit", moved_by)
        return
    waited = max(0.0, LOGIN_PACE_FLOOR_S - (time.monotonic() - pace.origin))
    deadline = time.monotonic() + waited
    try:
        # Sliced so a run canceled, or a page that moved, during the wait never gets the held gesture.
        while (left := deadline - time.monotonic()) > 0:
            await asyncio.sleep(min(left, _CANCEL_POLL_S))
            if await _cancel_seen(pace):
                pace.canceled = True
                _not_held(pace, "canceled_during_wait")
                raise LoginPaceRefused("the task was canceled during the login wait; the gesture was not sent")
            if moved_by := _moved_by(pace, anchor, realm):
                _not_held(pace, "page_moved_during_wait", moved_by)
                raise LoginPaceRefused(
                    "the page changed during the login wait, so the held click or key was not sent; "
                    "observe the page again"
                )
    except asyncio.CancelledError:
        # A caller's timeout (the captcha solver's checkbox click) must not use up the hold the submit needs.
        LOG.info("Task V3 login pace dwell interrupted", nav_s=round(time.monotonic() - pace.origin, 1))
        raise
    pace.done = True
    _unsubscribe(pace)
    LOG.info(
        "Task V3 login pace dwell",
        waited_s=round(waited, 1),
        nav_to_submit_s=round(time.monotonic() - pace.origin, 1),
    )


def end_login_pace() -> None:
    pace = _login_pace.get()
    if pace is not None and pace.anchor is not None and not pace.done:
        _not_held(pace, "no_click_or_enter_after_password")
    if pace is not None:
        _unsubscribe(pace)
    _login_pace.set(None)


def _opts(**options: Any) -> dict[str, Any]:
    # Only what the caller passed, so each call carries exactly the keywords the tool always sent.
    return {key: value for key, value in options.items() if value is not None}


async def click(
    realm: Realm,
    target: str | Locator,
    *,
    timeout: float | None = None,
    force: bool | None = None,
    position: dict[str, float] | None = None,
) -> None:
    options = _opts(timeout=timeout, force=force, position=position)
    await _hold_submit(realm)
    if isinstance(target, str):
        await realm.click(target, **options)
    else:
        await target.click(**options)


async def click_handle(
    realm: Realm,
    handle: ElementHandle,
    *,
    timeout: float | None = None,
    force: bool | None = None,
    position: dict[str, float] | None = None,
) -> None:
    await _hold_submit(realm)
    await handle.click(**_opts(timeout=timeout, force=force, position=position))


async def click_at(page: Page, x: float, y: float) -> None:
    await _hold_submit(page)
    await page.mouse.click(x, y)


async def hover(realm: Realm, selector: str, *, timeout: float | None = None) -> None:
    await realm.hover(selector, **_opts(timeout=timeout))


async def js_click(realm: Realm, root_query_js: str, arg: Any) -> Any:
    """Click a hidden native control from inside the page. No pointer events, as in v1."""
    await _hold_submit(realm)
    return await realm.evaluate(
        "(arg) => { const _q = "
        + root_query_js
        + "; const el = _q.find(arg.sel) || arg.el;"
        + " if (!el || !el.isConnected) return false; el.click(); return true; }",
        arg,
    )


async def focus(realm: Realm, target: str | Locator, *, timeout: float | None = None) -> None:
    if isinstance(target, str):
        await realm.focus(target, **_opts(timeout=timeout))
    else:
        await target.focus(**_opts(timeout=timeout))


async def fill(realm: Realm, target: Target, value: str, *, timeout: float | None = None) -> None:
    if isinstance(target, str):
        await realm.fill(target, value, **_opts(timeout=timeout))
    else:
        await target.fill(value, **_opts(timeout=timeout))


async def clear(realm: Realm, selector: str, *, timeout: float | None = None) -> None:
    await realm.fill(selector, "", **_opts(timeout=timeout))


async def type_keys(
    realm: Realm,
    target: str | Locator | None,
    text: str,
    *,
    delay: float | None = None,
    timeout: float | None = None,
) -> None:
    """Key events for ``text``. ``target`` None types into whatever holds focus, with no focus of its own;
    ``realm`` is then the page."""
    options = _opts(delay=delay, timeout=timeout)
    if target is None:
        await cast(Page, realm).keyboard.type(text, **options)
    elif isinstance(target, str):
        await realm.type(target, text, **options)
    else:
        await target.press_sequentially(text, **options)


async def press(
    realm: Realm, target: str | Locator | ElementHandle | None, key: str, *, timeout: float | None = None
) -> None:
    """A key press, raw as v1's keypress is. ``target`` None presses on ``realm``, the page's, keyboard."""
    options = _opts(timeout=timeout)
    if key.endswith("Enter"):
        await _hold_submit(realm)
    if target is None:
        await cast(Page, realm).keyboard.press(key, **options)
    elif isinstance(target, str):
        await realm.press(target, key, **options)
    else:
        await target.press(key, **options)


async def select_option(
    realm: Realm,
    selector: str,
    *,
    label: str | list[str] | None = None,
    value: str | list[str] | None = None,
    timeout: float | None = None,
    force: bool | None = None,
) -> None:
    if label is not None:
        await realm.select_option(selector, label=label, **_opts(timeout=timeout, force=force))
    else:
        await realm.select_option(selector, value=value, **_opts(timeout=timeout, force=force))


async def set_files(chooser: FileChooser, paths: list[str]) -> None:
    await chooser.set_files(paths)


async def set_input_files(target: Locator | ElementHandle, paths: list[str]) -> None:
    await target.set_input_files(paths)


async def wheel(page: Page, delta_x: float, delta_y: float) -> None:
    await page.mouse.wheel(delta_x, delta_y)
