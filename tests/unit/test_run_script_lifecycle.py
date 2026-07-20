"""Terminal-cleanup lifecycle of ``script_service.run_script``.

A standalone script pins its browser under ``script_id`` and must reclaim it at the script run's
terminal boundary (``cleanup_for_script``). The workflow-backed path keys its browser under
``workflow_run_id`` and is cleaned by the workflow, so ``run_script`` must skip script cleanup there.
Cleanup is best-effort: a cleanup/release failure must never replace the script's own exception.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.services.script_service import run_script
from skyvern.webeye.real_browser_manager import RealBrowserManager

SUCCESS_SCRIPT = "async def run_workflow(parameters=None):\n    return None\n"
FAILING_SCRIPT = "async def run_workflow(parameters=None):\n    raise RuntimeError('script boom')\n"
CANCEL_SCRIPT = "import asyncio\n\n\nasync def run_workflow(parameters=None):\n    raise asyncio.CancelledError()\n"
# Mimics setup() acquiring an explicit persistent session mid-run: it records the effective session on the
# run context, which terminal cleanup must then release (even though run_script itself received no session).
ACQUIRE_SESSION_SCRIPT = (
    "from skyvern.forge.sdk.core import skyvern_context\n\n\n"
    "async def run_workflow(parameters=None):\n"
    "    ctx = skyvern_context.current()\n"
    "    if ctx is not None:\n"
    "        ctx.browser_session_id = 'pbs_acquired'\n"
    "    return None\n"
)


@pytest.fixture(autouse=True)
def _reset_skyvern_context():
    yield
    skyvern_context.reset()


def _write_script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "user_script.py"
    path.write_text(body)
    return str(path)


@pytest.mark.asyncio
async def test_run_script_standalone_success_runs_terminal_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = MagicMock()
    manager.cleanup_for_script = AsyncMock()
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)
    path = _write_script(tmp_path, SUCCESS_SCRIPT)

    await run_script(path, script_id="scr_1", organization_id="org_1", browser_session_id="sess_1")

    manager.cleanup_for_script.assert_awaited_once_with("scr_1", browser_session_id="sess_1", organization_id="org_1")


@pytest.mark.asyncio
async def test_run_script_releases_effective_session_and_org_from_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # run_script gets neither session nor org, but a pre-existing context supplies the org and the script's
    # setup acquires a session (recorded on context). Terminal cleanup must source BOTH keys from context so
    # the acquired session is actually released under its org; raw None args would skip release entirely and
    # close the reusable browser.
    manager = MagicMock()
    manager.cleanup_for_script = AsyncMock()
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)
    path = _write_script(tmp_path, ACQUIRE_SESSION_SCRIPT)

    skyvern_context.set(skyvern_context.SkyvernContext(organization_id="org_ctx"))
    await run_script(path, script_id="scr_1", organization_id=None, browser_session_id=None)

    manager.cleanup_for_script.assert_awaited_once_with(
        "scr_1", browser_session_id="pbs_acquired", organization_id="org_ctx"
    )


@pytest.mark.asyncio
async def test_run_script_workflow_backed_skips_script_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A workflow-backed script (workflow_run_id set) is owned/cleaned by the workflow run, so run_script
    # must NOT invoke the standalone script cleanup.
    manager = MagicMock()
    manager.cleanup_for_script = AsyncMock()
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)
    database = MagicMock()
    workflow_run = MagicMock()
    database.workflow_runs.get_workflow_run = AsyncMock(return_value=workflow_run)
    database.workflow_runs.update_workflow_run = AsyncMock(return_value=workflow_run)
    monkeypatch.setattr(app, "DATABASE", database)
    path = _write_script(tmp_path, SUCCESS_SCRIPT)

    await run_script(path, script_id="scr_1", organization_id="org_1", workflow_run_id="wr_1")

    manager.cleanup_for_script.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_script_failure_with_release_failure_preserves_script_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # MF3: the script raises AND the persistent-session release fails during terminal cleanup. The real
    # cleanup_for_script swallows the release failure (best-effort), so the script's own error — not the
    # release error — is the exception that surfaces out of run_script's finally.
    manager = RealBrowserManager()
    state = MagicMock()
    state.close = AsyncMock()
    manager.pages["scr_1"] = state
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock(side_effect=RuntimeError("release boom"))
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", sessions)
    path = _write_script(tmp_path, FAILING_SCRIPT)

    with pytest.raises(RuntimeError, match="script boom"):
        await run_script(path, script_id="scr_1", organization_id="org_1", browser_session_id="sess_1")

    sessions.release_browser_session.assert_awaited_once()  # cleanup ran and swallowed the release failure
    assert "scr_1" not in manager.pages  # the page was still reclaimed


@pytest.mark.asyncio
async def test_run_script_persistent_session_closes_reusable_and_releases(monkeypatch, tmp_path: Path) -> None:
    # MF1 production path: run_script keeps close default True, yet persistent cleanup closes reusable then releases.
    manager = RealBrowserManager()
    state = MagicMock()
    state.close = AsyncMock()
    state.browser_context = None
    manager.pages["scr_1"] = state
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)
    sessions = MagicMock()
    sessions.release_browser_session = AsyncMock()
    monkeypatch.setattr(app, "PERSISTENT_SESSIONS_MANAGER", sessions)
    order = MagicMock()
    order.attach_mock(state.close, "close")
    order.attach_mock(sessions.release_browser_session, "release")
    path = _write_script(tmp_path, SUCCESS_SCRIPT)
    await run_script(path, script_id="scr_1", organization_id="org_1", browser_session_id="sess_1")
    state.close.assert_awaited_once_with(close_browser_on_completion=False, release_driver=False)
    sessions.release_browser_session.assert_awaited_once_with("sess_1", organization_id="org_1")
    assert [c[0] for c in order.method_calls] == ["close", "release"]
    assert "scr_1" not in manager.pages


# MF3: an ordinary cleanup failure must not replace the script's outcome — success/error/CancelledError preserved.
@pytest.mark.parametrize(
    "body, exc, match",
    [
        (SUCCESS_SCRIPT, None, None),
        (FAILING_SCRIPT, RuntimeError, "script boom"),
        (CANCEL_SCRIPT, asyncio.CancelledError, None),
    ],
)
@pytest.mark.asyncio
async def test_run_script_ordinary_cleanup_error_preserves_outcome(
    monkeypatch, tmp_path: Path, body, exc, match
) -> None:
    manager = MagicMock()
    manager.cleanup_for_script = AsyncMock(side_effect=RuntimeError("cleanup boom"))
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)
    path = _write_script(tmp_path, body)
    kwargs = dict(script_id="scr_1", organization_id="org_1", browser_session_id="sess_1")
    if exc:
        with pytest.raises(exc, match=match):
            await run_script(path, **kwargs)
    else:
        await run_script(path, **kwargs)
    manager.cleanup_for_script.assert_awaited_once()
