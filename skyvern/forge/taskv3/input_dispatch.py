"""The one place Task V3 sends pointer and keyboard input to a page.

Control runs the Playwright call each tool made before this module existed, argument for argument. The
TASK_V3_POINTER_PARITY treatment first moves the pointer onto the input or click target, as v1's action setup does.
"""

from __future__ import annotations

from typing import Any, TypeAlias, cast

import structlog
from playwright.async_api import ElementHandle, FileChooser, Frame, Locator, Page

from skyvern.config import settings
from skyvern.forge.sdk.event.factory import EventStrategyFactory
from skyvern.forge.taskv3.run_arms import POINTER_PARITY_FLAG, run_arm_enabled
from skyvern.webeye.browser_object_predicates import is_page_like

LOG = structlog.get_logger()

Realm: TypeAlias = Page | Frame
Target: TypeAlias = str | Locator | ElementHandle


def _opts(**options: Any) -> dict[str, Any]:
    # Only what the caller passed, so each call carries exactly the keywords the tool always sent.
    return {key: value for key, value in options.items() if value is not None}


def parity() -> bool:
    """Whether this run is on the TASK_V3_POINTER_PARITY treatment."""
    return run_arm_enabled(POINTER_PARITY_FLAG, settings.TASK_V3_POINTER_PARITY)


async def _pointer_onto(realm: Realm, target: Target, timeout: float | None) -> None:
    """Treatment: move the pointer onto ``target`` through the run's cursor strategy, as v1 does before it
    types or clicks. The strategy's own position then ends on the target the native gesture lands on."""
    if not parity():
        return
    try:
        page = cast(Page, realm) if is_page_like(realm) else cast(Frame, realm).page
        locator = realm.locator(target).first if isinstance(target, str) else target
        await locator.scroll_into_view_if_needed(timeout=min(2000, timeout or 2000))
        # An ElementHandle answers bounding_box() as a Locator does, which is all the strategies read.
        await EventStrategyFactory.move_to_element(page, cast(Locator, locator))
    except Exception:
        LOG.info("taskv3 pointer move skipped", exc_info=True)


async def click(
    realm: Realm,
    target: str | Locator,
    *,
    timeout: float | None = None,
    force: bool | None = None,
    position: dict[str, float] | None = None,
) -> None:
    options = _opts(timeout=timeout, force=force, position=position)
    await _pointer_onto(realm, target, timeout)
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
    await _pointer_onto(realm, handle, timeout)
    await handle.click(**_opts(timeout=timeout, force=force, position=position))


async def click_at(page: Page, x: float, y: float) -> None:
    if parity():
        try:
            await EventStrategyFactory.move_cursor(page, x, y)
        except Exception:
            LOG.info("taskv3 pointer move skipped", exc_info=True)
    await page.mouse.click(x, y)


async def hover(realm: Realm, selector: str, *, timeout: float | None = None) -> None:
    await _pointer_onto(realm, selector, timeout)
    await realm.hover(selector, **_opts(timeout=timeout))


async def js_click(realm: Realm, root_query_js: str, arg: Any) -> Any:
    """Click a hidden native control from inside the page. No pointer events, as in v1."""
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
    await _pointer_onto(realm, target, timeout)
    if isinstance(target, str):
        await realm.fill(target, value, **_opts(timeout=timeout))
    else:
        await target.fill(value, **_opts(timeout=timeout))


async def clear(realm: Realm, selector: str, *, timeout: float | None = None) -> None:
    await _pointer_onto(realm, selector, timeout)
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
    if target is not None:
        await _pointer_onto(realm, target, timeout)
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
