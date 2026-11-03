from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Annotated, Any

import structlog
from pydantic import Field

from skyvern.cli.core.browser_ops import do_screenshot
from skyvern.cli.core.guards import CREDENTIAL_HINT, PASSWORD_PATTERN, STALE_FRAME_HINT, GuardError
from skyvern.exceptions import StaleFrameSelectionError

from ._common import ErrorCode, make_error, make_result, save_artifact
from ._session import BrowserNotAvailableError, get_page, no_browser_error

LOG = structlog.get_logger(__name__)

# Walks open shadow roots because activeElement stops at the host element.
_FOCUSED_IS_PASSWORD_JS = """() => {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  return !!(el && el.tagName === 'INPUT' && el.type === 'password');
}"""

_PASSWORD_TARGET_MESSAGE = "Cannot write into password fields — credentials must not be passed through tool calls"
_MAX_GRID_ROWS = 100
_MAX_GRID_CELLS_PER_ROW = 50
_MAX_GRID_CELLS = 1000


async def _focused_element_is_password(page: Any) -> bool:
    try:
        return bool(await page.evaluate(_FOCUSED_IS_PASSWORD_JS))
    except Exception as exc:
        LOG.debug("write_grid_focus_password_check_failed", error=str(exc))
        return True


async def skyvern_write_grid(
    rows: Annotated[
        list[list[str]],
        Field(description="2-D array of cell values in row-major order", max_length=_MAX_GRID_ROWS),
    ],
    session_id: Annotated[str | None, Field(description="Browser session ID (pbs_...)")] = None,
    cdp_url: Annotated[str | None, Field(description="CDP WebSocket URL")] = None,
    focus_selector: Annotated[
        str | None, Field(description="Optional CSS selector to click before writing, such as the top-left cell")
    ] = None,
    focus_xy: Annotated[
        list[int] | None, Field(description="Optional [x, y] coordinates to click before writing, for canvas grids")
    ] = None,
    screenshot: Annotated[bool, Field(description="Capture a screenshot after writing the grid")] = True,
) -> dict[str, Any]:
    "Type an entire 2-D grid of values into the browser's currently focused spreadsheet or table in ONE call. Focus the top-left cell first (via focus_selector/focus_xy here, or a prior skyvern_click), then this types row-major using keyboard input: Tab advances columns, Enter advances rows. Far cheaper than typing or pasting cell-by-cell. Optionally captures a screenshot in the same call. Works on any grid/spreadsheet editor."
    if not rows:
        return make_result("skyvern_write_grid", data={"rows_written": 0, "cells": 0, "path": None})

    total_cells = sum(len(row) for row in rows)
    if (
        len(rows) > _MAX_GRID_ROWS
        or any(len(row) > _MAX_GRID_CELLS_PER_ROW for row in rows)
        or total_cells > _MAX_GRID_CELLS
    ):
        return make_result(
            "skyvern_write_grid",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "Grid exceeds the write limit",
                f"Use at most {_MAX_GRID_ROWS} rows, {_MAX_GRID_CELLS_PER_ROW} cells per row, "
                f"and {_MAX_GRID_CELLS} cells total.",
            ),
        )

    if focus_selector and PASSWORD_PATTERN.search(focus_selector):
        return make_result(
            "skyvern_write_grid",
            ok=False,
            error=make_error(ErrorCode.INVALID_INPUT, _PASSWORD_TARGET_MESSAGE, CREDENTIAL_HINT),
        )

    try:
        page, ctx = await get_page(session_id=session_id, cdp_url=cdp_url)
    except BrowserNotAvailableError as exc:
        return make_result("skyvern_write_grid", ok=False, error=no_browser_error(exc))

    try:
        # Page-space input ignores the frame selection; read the scope so an unowned one refuses
        # instead of writing into whichever document currently has focus.
        _ = page.locator_scope
    except StaleFrameSelectionError as exc:
        return make_result(
            "skyvern_write_grid",
            ok=False,
            error=make_error(ErrorCode.STALE_FRAME_SELECTION, str(exc), STALE_FRAME_HINT, exc=exc),
        )

    total = 0
    try:
        if focus_selector:
            await page.click(focus_selector)

        cdp = await page.page.context.new_cdp_session(page.page)
        try:
            if focus_xy and len(focus_xy) == 2:
                x, y = focus_xy
                await cdp.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
                )
                await cdp.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
                )
                await asyncio.sleep(0.3)

            for row in rows:
                for i, cell in enumerate(row):
                    if await _focused_element_is_password(page):
                        raise GuardError(_PASSWORD_TARGET_MESSAGE, CREDENTIAL_HINT)
                    await cdp.send("Input.insertText", {"text": str(cell)})
                    total += 1
                    if i < len(row) - 1:
                        await cdp.send(
                            "Input.dispatchKeyEvent",
                            {"type": "keyDown", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
                        )
                        await cdp.send(
                            "Input.dispatchKeyEvent",
                            {"type": "keyUp", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
                        )
                await cdp.send(
                    "Input.dispatchKeyEvent",
                    {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
                )
                await cdp.send(
                    "Input.dispatchKeyEvent",
                    {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
                )
                await asyncio.sleep(0.03)
        finally:
            with contextlib.suppress(Exception):
                await cdp.detach()
    except GuardError as e:
        return make_result(
            "skyvern_write_grid",
            ok=False,
            browser_context=ctx,
            error=make_error(ErrorCode.INVALID_INPUT, str(e), e.hint, exc=e),
        )
    except Exception as e:
        LOG.warning("skyvern_write_grid_failed", error=str(e), exc_info=True)
        return make_result(
            "skyvern_write_grid",
            ok=False,
            browser_context=ctx,
            error=make_error(
                ErrorCode.ACTION_FAILED, str(e), "Check that the target grid is focused and editable", exc=e
            ),
        )

    shot_path = None
    artifact = None
    if screenshot:
        try:
            await asyncio.sleep(0.5)
            result = await do_screenshot(page)
            ts = datetime.now(timezone.utc).strftime("%H%M%S_%f")
            artifact = save_artifact(
                result.data,
                kind="screenshot",
                filename=f"write_grid_{ts}.png",
                mime="image/png",
                session_id=ctx.session_id,
            )
            shot_path = artifact.path
        except Exception as e:
            LOG.warning("skyvern_write_grid_screenshot_failed", error=str(e), exc_info=True)

    data = {"rows_written": len(rows), "cells": total, "path": shot_path}
    if artifact:
        return make_result("skyvern_write_grid", browser_context=ctx, data=data, artifacts=[artifact])
    return make_result("skyvern_write_grid", browser_context=ctx, data=data)
