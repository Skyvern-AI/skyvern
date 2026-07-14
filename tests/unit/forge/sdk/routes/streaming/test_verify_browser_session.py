"""Tests for verify_browser_session — local-mode short-circuit (SKY-8017)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.routes.streaming.verify import verify_browser_session, verify_task, verify_workflow_run
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    PersistentBrowserSession,
    PersistentBrowserSessionStatus,
)
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


def _make_session(
    *,
    browser_address: str | None,
    status: PersistentBrowserSessionStatus,
    vnc_port: int | None = None,
) -> PersistentBrowserSession:
    now = datetime.now(timezone.utc)
    return PersistentBrowserSession(
        persistent_browser_session_id="bs_test",
        organization_id="o_test",
        status=status,
        browser_address=browser_address,
        vnc_port=vnc_port,
        created_at=now,
        modified_at=now,
    )


class TestVerifyBrowserSessionLocalShortCircuit:
    @pytest.mark.asyncio
    async def test_short_circuits_in_cdp_mode_with_no_address(self) -> None:
        # Local CDP mode: browser runs in-process, browser_address is never
        # populated. Verify must not block on the address wait.
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
            vnc_port=6087,
        )

        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)
        manager.get_browser_address = AsyncMock(
            side_effect=AssertionError("must not wait for address in cdp mode"),
        )

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "cdp"
            result = await verify_browser_session("bs_test", "o_test")

        assert result is not None
        assert result.browser_address == ""
        assert result.persistent_browser_session_id == "bs_test"
        manager.get_browser_address.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_circuits_address_lookup_for_addressless_local_vnc_session(self) -> None:
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
            vnc_port=6087,
        )

        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)
        manager.get_browser_address_if_ready = AsyncMock(
            side_effect=AssertionError("must not look up an address for a local VNC session"),
        )
        manager.get_browser_address = AsyncMock(
            side_effect=AssertionError("must not wait for an address for a local VNC session"),
        )

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "vnc"
            settings_mock.ENV = "local"
            result = await verify_browser_session("bs_test", "o_test")

        assert result is not None
        assert result.browser_address == ""
        assert result.vnc_port == 6087
        manager.get_browser_address_if_ready.assert_not_awaited()
        manager.get_browser_address.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_checks_address_readiness_in_non_cdp_mode(self) -> None:
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
        )

        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)
        manager.get_browser_address_if_ready = AsyncMock(return_value="ws://remote:9222")
        manager.get_browser_address = AsyncMock(
            side_effect=AssertionError("must not enter blocking address wait"),
        )

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "vnc"
            settings_mock.ENV = "local"
            result = await verify_browser_session("bs_test", "o_test")

        assert result is not None
        assert result.browser_address == "ws://remote:9222"
        manager.get_browser_address_if_ready.assert_awaited_once_with(
            session_id="bs_test",
            organization_id="o_test",
        )
        manager.get_browser_address.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocks_until_address_ready_in_non_local_non_cdp_mode(self) -> None:
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
        )

        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)
        manager.get_browser_address = AsyncMock(return_value="ws://remote:9222")
        manager.get_browser_address_if_ready = AsyncMock(
            side_effect=AssertionError("must not use non-blocking address check in production"),
        )

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "vnc"
            settings_mock.ENV = "production"
            result = await verify_browser_session("bs_test", "o_test")

        assert result is not None
        assert result.browser_address == "ws://remote:9222"
        manager.get_browser_address.assert_awaited_once_with(
            session_id="bs_test",
            organization_id="o_test",
        )
        manager.get_browser_address_if_ready.assert_not_called()


class TestVerifyRunnableLocalVncSession:
    @pytest.mark.asyncio
    async def test_task_accepts_addressless_local_vnc_session(self) -> None:
        task = SimpleNamespace(task_id="task_test", status=TaskStatus.running)
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
            vnc_port=6087,
        )
        manager = MagicMock()
        manager.get_session_by_runnable_id = AsyncMock(return_value=session)

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.DATABASE.tasks.get_task = AsyncMock(return_value=task)
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "vnc"
            verified_task, verified_session = await verify_task("task_test", "o_test")

        assert verified_task is task
        assert verified_session is not None
        assert verified_session.browser_address == ""
        assert verified_session.vnc_port == 6087

    @pytest.mark.asyncio
    async def test_task_preserves_addressless_rejection_outside_local_vnc(self) -> None:
        task = SimpleNamespace(task_id="task_test", status=TaskStatus.running)
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
            vnc_port=6087,
        )
        manager = MagicMock()
        manager.get_session_by_runnable_id = AsyncMock(return_value=session)

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.DATABASE.tasks.get_task = AsyncMock(return_value=task)
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "cdp"
            verified_task, verified_session = await verify_task("task_test", "o_test")

        assert verified_task is task
        assert verified_session is None

    @pytest.mark.asyncio
    async def test_workflow_accepts_addressless_local_vnc_session_without_waiting(self) -> None:
        workflow_run = SimpleNamespace(
            workflow_run_id="wr_test",
            status=WorkflowRunStatus.running,
        )
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
            vnc_port=6087,
        )
        manager = MagicMock()
        manager.get_session_by_runnable_id = AsyncMock(return_value=session)
        manager.get_browser_address = AsyncMock(
            side_effect=AssertionError("must not wait for an address for a local VNC session"),
        )

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=workflow_run)
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "vnc"
            verified_run, verified_session = await verify_workflow_run("wr_test", "o_test")

        assert verified_run is workflow_run
        assert verified_session is not None
        assert verified_session.browser_address == ""
        assert verified_session.vnc_port == 6087
        manager.get_browser_address.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_workflow_preserves_blocking_address_lookup_without_local_vnc_port(self) -> None:
        workflow_run = SimpleNamespace(
            workflow_run_id="wr_test",
            status=WorkflowRunStatus.running,
        )
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
        )
        manager = MagicMock()
        manager.get_session_by_runnable_id = AsyncMock(return_value=session)
        manager.get_browser_address = AsyncMock(return_value="ws://remote:9222")

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=workflow_run)
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "vnc"
            verified_run, verified_session = await verify_workflow_run("wr_test", "o_test")

        assert verified_run is workflow_run
        assert verified_session is not None
        assert verified_session.browser_address == "ws://remote:9222"
        manager.get_browser_address.assert_awaited_once_with(session_id="bs_test", organization_id="o_test")


class TestVerifyBrowserSessionAddressFallback:
    @pytest.mark.asyncio
    async def test_returns_none_when_address_not_ready_in_non_cdp_mode(self) -> None:
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
        )

        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)
        manager.get_browser_address_if_ready = AsyncMock(return_value=None)
        manager.get_browser_address = AsyncMock(
            side_effect=AssertionError("must not enter blocking address wait"),
        )

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "vnc"
            settings_mock.ENV = "local"
            result = await verify_browser_session("bs_test", "o_test")

        assert result is None
        manager.get_browser_address_if_ready.assert_awaited_once_with(session_id="bs_test", organization_id="o_test")
        manager.get_browser_address.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_existing_address_without_polling(self) -> None:
        session = _make_session(
            browser_address="ws://remote:9222",
            status=PersistentBrowserSessionStatus.running,
        )

        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)
        manager.get_browser_address = AsyncMock(
            side_effect=AssertionError("must not wait when address present"),
        )

        with patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock:
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            result = await verify_browser_session("bs_test", "o_test")

        assert result is not None
        assert result.browser_address == "ws://remote:9222"
