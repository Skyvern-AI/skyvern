"""The one place Task V3 sends pointer and keyboard input to a page.

Control runs the Playwright call each tool made before this module existed, argument for argument. Treatment
sends a gesture through EventStrategyFactory wherever v1 does and calls Playwright directly wherever v1 does, so
the run follows whatever USE_EVENT_STRATEGIES registered for the process, like a v1 block of the same run.
"""

from __future__ import annotations

import asyncio
from typing import Any, TypeAlias, cast

import structlog
from playwright.async_api import ElementHandle, FileChooser, Frame, Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.config import settings
from skyvern.constants import TEXT_PRESS_MAX_LENGTH
from skyvern.forge.sdk.event.factory import EventStrategyFactory
from skyvern.forge.taskv3.run_arms import HUMANIZED_INPUT_FLAG, current_run_arm, run_arm_enabled
from skyvern.webeye.browser_object_predicates import is_page_like

LOG = structlog.get_logger()

Realm: TypeAlias = Page | Frame
Target: TypeAlias = str | Locator | ElementHandle

# After the cursor reaches a target, wait until the page has painted twice and then gone this long without a DOM
# mutation, so a hover-intent menu crossed on the way has finished opening or closing before a caller takes its
# pre-action baseline. Capped: a page that never stops mutating (a ticker, a spinner) costs the cap, not a stall.
_SETTLE_QUIET_MS = 150
_SETTLE_CAP_MS = 1000
_SETTLE_JS = r"""(arg) => new Promise((resolve) => {
  const started = performance.now();
  let last = started;
  const obs = new MutationObserver(() => { last = performance.now(); });
  const tick = () => {
    const now = performance.now();
    if (now - last >= arg.quietMs || now - started >= arg.capMs) { obs.disconnect(); resolve(true); return; }
    setTimeout(tick, 25);
  };
  requestAnimationFrame(() => requestAnimationFrame(() => {
    obs.observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
    last = performance.now();
    setTimeout(tick, 25);
  }));
  setTimeout(() => { obs.disconnect(); resolve(false); }, arg.capMs);
})"""


def arm_fields() -> dict[str, Any]:
    return {
        "humanized_input_arm": current_run_arm(HUMANIZED_INPUT_FLAG),
        "humanized_input_in_effect": humanized(),
        "global_event_strategy_profile": EventStrategyFactory.registered_profile(),
    }


def humanized() -> bool:
    return run_arm_enabled(HUMANIZED_INPUT_FLAG, settings.TASK_V3_HUMANIZED_INPUT)


def _opts(**options: Any) -> dict[str, Any]:
    # Only what the caller passed, so a control call carries exactly the keywords the tool always sent.
    return {key: value for key, value in options.items() if value is not None}


def _page_of(realm: Realm) -> Page:
    # A Frame has no mouse or keyboard; the factory always drives the tab that owns it.
    return cast(Page, realm) if is_page_like(realm) else cast(Frame, realm).page


def _locator(realm: Realm, target: str | Locator) -> Locator:
    # A page-level selector method acts on the first match; a locator is strict unless told otherwise.
    return realm.locator(target).first if isinstance(target, str) else target


async def _move_to_element(page: Page, element: ElementHandle) -> None:
    try:
        box = await element.bounding_box()
        if box is None:
            return
        await EventStrategyFactory.move_cursor(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    except Exception:
        LOG.debug("taskv3 cursor move to a handed-in element failed, clicking anyway", exc_info=True)


async def approach(realm: Realm, target: str | Locator) -> None:
    """Treatment: bring the cursor to ``target`` and let the page settle, so travel effects predate a baseline."""
    if not humanized():
        return
    locator = _locator(realm, target)
    try:
        # The cursor is clamped to the viewport, so an off-screen target would otherwise be scrolled to, and
        # travelled to again, only after the baseline.
        await locator.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        LOG.debug("taskv3 scroll before cursor approach failed", exc_info=True)
    await EventStrategyFactory.move_to_element(locator.page, locator)
    try:
        async with asyncio.timeout((_SETTLE_CAP_MS + 500) / 1000):
            await realm.evaluate(_SETTLE_JS, {"quietMs": _SETTLE_QUIET_MS, "capMs": _SETTLE_CAP_MS})
    except Exception:
        LOG.debug("taskv3 settle after cursor approach did not complete", exc_info=True)


async def click(
    realm: Realm,
    target: str | Locator,
    *,
    timeout: float | None = None,
    force: bool | None = None,
    position: dict[str, float] | None = None,
) -> None:
    options = _opts(timeout=timeout, force=force, position=position)
    if not humanized():
        if isinstance(target, str):
            await realm.click(target, **options)
        else:
            await target.click(**options)
        return
    locator = _locator(realm, target)
    if force is not None or position is not None:
        await EventStrategyFactory.move_to_element(locator.page, locator)
        if isinstance(target, str):
            await realm.click(target, **options)
        else:
            await target.click(**options)
        return
    await EventStrategyFactory.click_element(locator.page, locator, timeout=timeout)


async def click_handle(
    realm: Realm,
    handle: ElementHandle,
    *,
    timeout: float | None = None,
    force: bool | None = None,
    position: dict[str, float] | None = None,
) -> None:
    if humanized():
        # v1's pattern for a click the cursor strategy cannot express: move there, then click directly.
        await _move_to_element(_page_of(realm), handle)
    await handle.click(**_opts(timeout=timeout, force=force, position=position))


async def click_at(page: Page, x: float, y: float) -> None:
    if humanized():
        await EventStrategyFactory.move_cursor(page, x, y)
    await page.mouse.click(x, y)


async def hover(realm: Realm, selector: str, *, timeout: float | None = None) -> None:
    if humanized():
        locator = _locator(realm, selector)
        await EventStrategyFactory.move_to_element(locator.page, locator)
    await realm.hover(selector, **_opts(timeout=timeout))


async def js_click(realm: Realm, root_query_js: str, arg: Any) -> Any:
    """Click a hidden native control from inside the page. No pointer events, in either arm, as in v1."""
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
    """An atomic write, in both arms: v1 fills plain fields and secrets in one call and never humanizes them."""
    if isinstance(target, str):
        await realm.fill(target, value, **_opts(timeout=timeout))
    else:
        await target.fill(value, **_opts(timeout=timeout))


async def clear(realm: Realm, selector: str, *, timeout: float | None = None) -> None:
    if not humanized():
        await realm.fill(selector, "", **_opts(timeout=timeout))
        return
    locator = _locator(realm, selector)
    await EventStrategyFactory.clear_field(locator.page, locator, 0, timeout=timeout)


async def type_keys(
    realm: Realm,
    target: str | Locator | None,
    text: str,
    *,
    delay: float | None = None,
    timeout: float | None = None,
    secret: bool = False,
    replace: bool = False,
) -> None:
    """Key events for ``text``. ``target`` None types into whatever holds focus, with no focus of its own;
    ``realm`` is then the page.

    A secret is typed key by key as before in both arms: a humanized keyboard mistypes and backspaces, and a
    fill would not advance split code boxes. ``replace`` says the caller has just emptied the field, so
    treatment may write the untyped part with fill."""
    options = _opts(delay=delay, timeout=timeout)
    if secret or not humanized():
        if target is None:
            await cast(Page, realm).keyboard.type(text, **options)
        elif isinstance(target, str):
            await realm.type(target, text, **options)
        else:
            await target.press_sequentially(text, **options)
        return
    # As v1 types: all but the last few characters in one write, the tail through the registered strategy,
    # which owns the cadence. An emptied field takes the head as a fill; one being appended to takes it as
    # inserted text at the caret, since a fill would replace what the field already holds.
    cut = max(len(text) - TEXT_PRESS_MAX_LENGTH, 0)
    head, tail = text[:cut], text[cut:]
    page = cast(Page, realm) if target is None else _page_of(realm)
    seconds = None if not timeout else timeout / 1000
    try:
        async with asyncio.timeout(seconds):
            if head and replace and target is not None:
                await fill(realm, target, head, timeout=timeout)
            elif target is not None:
                # Focused once: a humanized keyboard given a locator re-focuses the field before every key,
                # which puts the caret back at the start of a select-on-focus field.
                await focus(realm, target, timeout=timeout)
            if head and not (replace and target is not None):
                await page.keyboard.insert_text(head)
            await EventStrategyFactory.type_text(page, None, tail, timeout=timeout)
    except TimeoutError as exc:
        raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded.") from exc


async def press(
    realm: Realm, target: str | Locator | ElementHandle | None, key: str, *, timeout: float | None = None
) -> None:
    """A key press, raw in both arms as v1's keypress is. ``target`` None presses on ``realm``, the page's, keyboard."""
    options = _opts(timeout=timeout)
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
    if humanized():
        await EventStrategyFactory.scroll_by(page, delta_x, delta_y)
    else:
        await page.mouse.wheel(delta_x, delta_y)
