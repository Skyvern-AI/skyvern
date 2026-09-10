"""``_connect_to_cdp_browser`` stamps ``needs_cdp_frame_publisher``.

It is the single chokepoint for remote-CDP creation here — ``cdp-connect``
always, plus ``chromium-headless`` / ``chromium-headful`` when
``browser_address`` is set — so one stamp there covers every remote-CDP path.
Ordinary local creators leave the marker False; the factory does not
auto-stamp.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import BrowserContext, Locator, Page

from skyvern.forge.sdk.workflow.models.code_block_recorder import RecordingPage
from skyvern.webeye import browser_factory as factory_module
from skyvern.webeye import display_recorder as dr
from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact
from skyvern.webeye.browser_factory import BrowserContextFactory
from skyvern.webeye.playwright_input import playwright_input_defaults_for_page


@pytest.mark.asyncio
async def test_connect_to_cdp_browser_stamps_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chokepoint stamps; every remote-CDP path inherits the marker."""
    fake_context = MagicMock()
    fake_browser = MagicMock()
    fake_browser.contexts = [fake_context]

    monkeypatch.setattr(
        factory_module,
        "_connect_over_cdp_with_diagnostics",
        AsyncMock(return_value=fake_browser),
    )

    _, browser_artifacts, _ = await factory_module._connect_to_cdp_browser(
        playwright=MagicMock(),
        remote_browser_url="ws://remote.example/cdp",
    )

    assert browser_artifacts.needs_cdp_frame_publisher is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "creator_name",
    ["_create_headless_chromium", "_create_headful_chromium", "_create_cdp_connection_browser"],
)
async def test_oss_creators_validate_caller_browser_address(
    monkeypatch: pytest.MonkeyPatch,
    creator_name: str,
) -> None:
    connect = AsyncMock(return_value=(MagicMock(), MagicMock(), None))
    monkeypatch.setattr(factory_module, "_connect_to_cdp_browser", connect)

    await getattr(factory_module, creator_name)(
        playwright=MagicMock(),
        browser_address="wss://browser.example.test/devtools/browser/id",
    )

    assert connect.await_args.kwargs["validate_browser_address"] is True


@pytest.mark.asyncio
async def test_cdp_connect_creator_trusts_configured_browser_address(monkeypatch: pytest.MonkeyPatch) -> None:
    connect = AsyncMock(return_value=(MagicMock(), MagicMock(), None))
    monkeypatch.setattr(factory_module, "_connect_to_cdp_browser", connect)
    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", "cdp-connect")
    monkeypatch.setattr(factory_module.settings, "CHROME_EXECUTABLE_PATH", None)

    await factory_module._create_cdp_connection_browser(playwright=MagicMock())

    assert connect.await_args.kwargs["validate_browser_address"] is False


@pytest.mark.asyncio
async def test_ordinary_local_creator_leaves_marker_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory does not auto-stamp; a local creator's marker stays False."""

    async def _local_creator(playwright: Any, **kwargs: Any) -> tuple[Any, BrowserArtifacts, None]:
        return object(), BrowserArtifacts(), None

    monkeypatch.setattr(factory_module, "restore_session_cookies", AsyncMock())
    monkeypatch.setattr(factory_module, "restore_banked_cookies", AsyncMock())
    monkeypatch.setattr(factory_module, "set_browser_console_log", lambda **_: None)
    monkeypatch.setattr(factory_module, "set_popup_video_listener", lambda **_: None)
    monkeypatch.setattr(factory_module, "set_download_file_listener", lambda **_: None)
    monkeypatch.setattr(factory_module, "set_dialog_handler", lambda **_: None)

    class _FakeAgentFunction:
        async def setup_browser_context_extensions(self, **_: Any) -> None:
            return None

        async def should_apply_banked_cookies(self, organization_id: str | None) -> bool:
            return False

    class _FakeApp:
        AGENT_FUNCTION = _FakeAgentFunction()

    monkeypatch.setattr(factory_module, "app", _FakeApp())

    BrowserContextFactory.register_type("test-local", _local_creator)
    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", "test-local")

    _, artifacts, _ = await BrowserContextFactory.create_browser_context(playwright=object())

    assert artifacts.needs_cdp_frame_publisher is False


def _factory_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory_module, "restore_session_cookies", AsyncMock())
    monkeypatch.setattr(factory_module, "restore_banked_cookies", AsyncMock())
    monkeypatch.setattr(factory_module, "set_browser_console_log", lambda **_: None)
    monkeypatch.setattr(factory_module, "set_popup_video_listener", lambda **_: None)
    monkeypatch.setattr(factory_module, "set_download_file_listener", lambda **_: None)
    monkeypatch.setattr(factory_module, "set_dialog_handler", lambda **_: None)

    class _FakeAgentFunction:
        async def setup_browser_context_extensions(self, **_: Any) -> None:
            return None

        async def should_apply_banked_cookies(self, organization_id: str | None) -> bool:
            return False

    class _FakeApp:
        AGENT_FUNCTION = _FakeAgentFunction()

    monkeypatch.setattr(factory_module, "app", _FakeApp())


@pytest.mark.parametrize(
    "recorder, eligible, expect_listener",
    [
        (None, True, True),  # eligible but acquisition REFUSED -> real call site MUST register Playwright fallback
        (object(), False, False),  # whole-display recorder acquired -> suppress Playwright (no dual recording)
    ],
    ids=["refused_registers_fallback", "acquired_suppresses"],
)
@pytest.mark.asyncio
async def test_create_browser_context_gates_playwright_video_on_acquired_recorder(
    monkeypatch: pytest.MonkeyPatch, recorder: object | None, eligible: bool, expect_listener: bool
) -> None:
    """Drives the REAL create_browser_context call site: the Playwright video listener registers iff no
    whole-display recorder was acquired, so an eligible-but-refused acquisition still falls back to Playwright."""
    listener = MagicMock()
    _factory_harness(monkeypatch)
    monkeypatch.setattr(factory_module, "set_popup_video_listener", listener)
    monkeypatch.setattr(factory_module, "attach_only_enforcing", lambda: False)

    async def _creator(playwright: Any, **kwargs: Any) -> tuple[Any, BrowserArtifacts, None]:
        artifacts = BrowserArtifacts()
        artifacts.local_display_recording_eligible = eligible
        artifacts._display_recorder = recorder
        return MagicMock(), artifacts, None

    BrowserContextFactory.register_type("test-recorder-gate", _creator)
    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", "test-recorder-gate")

    await BrowserContextFactory.create_browser_context(playwright=object())

    assert listener.called is expect_listener


@pytest.mark.asyncio
async def test_factory_registers_authoritative_playwright_input_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    context = MagicMock()

    async def _creator(playwright: Any, **kwargs: Any) -> tuple[Any, BrowserArtifacts, None]:
        return context, BrowserArtifacts(), None

    _factory_harness(monkeypatch)
    register_defaults = MagicMock()
    monkeypatch.setattr(factory_module, "register_playwright_input_context", register_defaults)
    BrowserContextFactory.register_type("test-input-defaults", _creator)
    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", "test-input-defaults")

    await BrowserContextFactory.create_browser_context(playwright=object())

    register_defaults.assert_called_once_with(context, strict_selectors=False)


@pytest.mark.asyncio
async def test_factory_preserves_context_strictness_for_recorder_omitted_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = MagicMock(spec=BrowserContext)
    raw_page = MagicMock(spec=Page)
    raw_page.context = context
    strict_locator = MagicMock(spec=Locator)
    strict_locator.page = raw_page
    strict_locator.first = MagicMock(spec=Locator)
    raw_page.locator.return_value = strict_locator

    async def _creator(playwright: Any, **kwargs: Any) -> tuple[Any, BrowserArtifacts, None]:
        assert kwargs["strict_selectors"] is True
        return context, BrowserArtifacts(), None

    _factory_harness(monkeypatch)
    strategy_aware_input = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.models.code_block_recorder.strategy_aware_input",
        strategy_aware_input,
    )
    BrowserContextFactory.register_type("test-strict-input-defaults", _creator)
    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", "test-strict-input-defaults")

    await BrowserContextFactory.create_browser_context(playwright=object(), strict_selectors=True)
    recording_page = RecordingPage(
        raw_page,
        strategy_aware_typing=True,
        playwright_input_defaults=playwright_input_defaults_for_page(raw_page),
    )

    await recording_page.fill("#multiple", "value")

    strategy_aware_input.assert_awaited_once_with(
        strict_locator,
        "value",
        clear=True,
        timeout=30_000,
    )


@pytest.mark.asyncio
async def test_factory_warns_when_requested_profile_not_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _profile_blind_creator(playwright: Any, **kwargs: Any) -> tuple[Any, BrowserArtifacts, None]:
        # Mirrors remote/vendor creators: accepts browser_profile_id but never applies it.
        return object(), BrowserArtifacts(), None

    _factory_harness(monkeypatch)
    log = MagicMock()
    monkeypatch.setattr(factory_module, "LOG", log)

    BrowserContextFactory.register_type("test-profile-blind", _profile_blind_creator)
    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", "test-profile-blind")

    await BrowserContextFactory.create_browser_context(
        playwright=object(), browser_profile_id="bp_x", organization_id="o_x"
    )

    assert any("not applied" in str(call.args[0]) for call in log.warning.call_args_list)


@pytest.mark.asyncio
async def test_factory_stays_quiet_when_requested_profile_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _profile_applying_creator(playwright: Any, **kwargs: Any) -> tuple[Any, BrowserArtifacts, None]:
        return object(), BrowserArtifacts(applied_browser_profile_id=str(kwargs.get("browser_profile_id"))), None

    _factory_harness(monkeypatch)
    log = MagicMock()
    monkeypatch.setattr(factory_module, "LOG", log)

    BrowserContextFactory.register_type("test-profile-applying", _profile_applying_creator)
    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", "test-profile-applying")

    await BrowserContextFactory.create_browser_context(
        playwright=object(), browser_profile_id="bp_x", organization_id="o_x"
    )

    assert not any("not applied" in str(call.args[0]) for call in log.warning.call_args_list)


@pytest.mark.asyncio
async def test_headless_chromium_stamps_applied_browser_profile_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from skyvern.forge import app

    monkeypatch.setattr(app.STORAGE, "retrieve_browser_profile", AsyncMock(return_value=str(tmp_path / "profile")))
    monkeypatch.setattr(BrowserContextFactory, "update_chromium_browser_preferences", MagicMock())
    monkeypatch.setattr(
        BrowserContextFactory,
        "build_browser_args",
        MagicMock(return_value={"record_har_path": str(tmp_path / "h.har")}),
    )
    monkeypatch.setattr(factory_module, "initialize_download_dir", lambda: str(tmp_path / "downloads"))
    playwright = MagicMock()
    playwright.chromium.launch_persistent_context = AsyncMock(return_value=MagicMock())

    _, artifacts, _ = await factory_module._create_headless_chromium(
        playwright,
        browser_profile_id="bp_test",
        organization_id="o_test",
    )
    assert artifacts.applied_browser_profile_id == "bp_test"

    # Storage miss: the creator falls back to a temp dir and the field stays None.
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_profile", AsyncMock(return_value=None))
    monkeypatch.setattr(factory_module, "make_temp_directory", lambda **_: str(tmp_path / "fresh"))
    _, artifacts_no_profile, _ = await factory_module._create_headless_chromium(
        playwright,
        browser_profile_id="bp_test",
        organization_id="o_test",
    )
    assert artifacts_no_profile.applied_browser_profile_id is None


@pytest.mark.asyncio
async def test_bootstrap_error_propagates_unwrapped_but_other_errors_are_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A creator's ``BrowserEngineBootstrapError`` (the narrow engine-boot marker) must reach the
    acquisition boundary unchanged so it can drive a one-hop engine fallback; an ordinary creator error
    is still wrapped in ``UnknownErrorWhileCreatingBrowserContext`` as before."""
    from skyvern.exceptions import UnknownErrorWhileCreatingBrowserContext
    from skyvern.webeye.browser_engine import BrowserEngineBootstrapError

    async def _bootstrap_failing_creator(playwright: Any, **kwargs: Any) -> tuple[Any, Any, None]:
        raise BrowserEngineBootstrapError("rustwright launch failed")

    async def _generic_failing_creator(playwright: Any, **kwargs: Any) -> tuple[Any, Any, None]:
        raise RuntimeError("some unrelated context failure")

    BrowserContextFactory.register_type("test-bootstrap-fail", _bootstrap_failing_creator)
    BrowserContextFactory.register_type("test-generic-fail", _generic_failing_creator)

    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", "test-bootstrap-fail")
    with pytest.raises(BrowserEngineBootstrapError):
        await BrowserContextFactory.create_browser_context(playwright=object())

    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", "test-generic-fail")
    with pytest.raises(UnknownErrorWhileCreatingBrowserContext):
        await BrowserContextFactory.create_browser_context(playwright=object())


@pytest.mark.asyncio
async def test_headful_creator_releases_recorder_when_launch_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # P1: a Temporal cancel (BaseException) landing on the pending launch_persistent_context must release the
    # already-started whole-display recorder — otherwise the bridge+ffmpeg keep recording and the display flock
    # and _REGISTRY entry leak pod-wide. RED at head: except-Exception does not catch CancelledError.
    monkeypatch.setattr(factory_module, "make_temp_directory", lambda **_: str(tmp_path / "ud"))
    monkeypatch.setattr(factory_module, "initialize_download_dir", lambda: str(tmp_path / "dl"))
    monkeypatch.setattr(BrowserContextFactory, "update_chromium_browser_preferences", MagicMock())
    monkeypatch.setattr(
        BrowserContextFactory,
        "build_browser_args",
        MagicMock(return_value={"record_har_path": str(tmp_path / "h.har")}),
    )

    rec = dr.DisplayRecorder(
        display=":99",
        owner_id="wr_t1",
        process=SimpleNamespace(returncode=0),  # already exited: stop() completes without signalling
        lock_fd=-1,
        video_artifact=VideoArtifact(video_path=str(tmp_path / "r.mp4")),
    )
    dr._REGISTRY[(":99", "wr_t1")] = rec

    async def _fake_prepare(browser_args: dict, browser_artifacts: BrowserArtifacts, **_: Any) -> None:
        browser_artifacts._display_recorder = rec
        browser_artifacts.video_artifacts = [rec.video_artifact]
        browser_artifacts._display_recorder_acquisition = dr.DisplayRecorderAcquisition(rec, rec.video_artifact, True)

    monkeypatch.setattr(factory_module, "prepare_local_display_recording", _fake_prepare)
    playwright = MagicMock()
    playwright.chromium.launch_persistent_context = AsyncMock(side_effect=asyncio.CancelledError())

    try:
        with pytest.raises(asyncio.CancelledError):
            await factory_module._create_headful_chromium(playwright, workflow_run_id="wr_t1")
        assert (":99", "wr_t1") not in dr._REGISTRY  # released + deregistered, not orphaned
        assert rec.is_stopped
    finally:
        dr._REGISTRY.pop((":99", "wr_t1"), None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "release_cancels, expected_exc",
    [
        (False, Exception),  # ordinary post-return error -> close before release, cleanup runs, error wraps
        (True, asyncio.CancelledError),  # a release cancel: cleanup_func STILL runs once, then cancellation wins
    ],
    ids=["ordinary_error", "release_cancel_runs_cleanup"],
)
async def test_factory_closes_context_before_releasing_recorder_on_post_return_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, release_cancels: bool, expected_exc: type[BaseException]
) -> None:
    # A post-return setup failure must unmap the tenant's window (context.close) BEFORE the display fence is
    # released (freeing the fence while mapped lets a later different-owner acquire capture it), and cleanup_func
    # must run exactly once afterward. A CancelledError from the release still runs cleanup_func, then wins.
    order: list[str] = []
    _factory_harness(monkeypatch)

    rec = dr.DisplayRecorder(
        display=":99",
        owner_id="wr_t1",
        process=SimpleNamespace(returncode=0),  # already exited: stop() completes without signalling
        lock_fd=-1,
        video_artifact=VideoArtifact(video_path=str(tmp_path / "r.mp4")),
    )
    dr._REGISTRY[(":99", "wr_t1")] = rec

    fake_context = AsyncMock()

    async def _close() -> None:
        order.append("close")

    fake_context.close = _close
    cleanup = AsyncMock(side_effect=lambda: order.append("cleanup"))

    async def _creator(playwright: Any, **kwargs: Any) -> tuple[Any, BrowserArtifacts, Any]:
        artifacts = BrowserArtifacts()
        artifacts._display_recorder = rec
        artifacts._display_recorder_acquisition = dr.DisplayRecorderAcquisition(rec, rec.video_artifact, True)
        return fake_context, artifacts, cleanup

    BrowserContextFactory.register_type("test-close-before-release", _creator)
    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", "test-close-before-release")

    real_release = factory_module.release_display_recorder

    async def _release(recorder: Any) -> None:
        order.append("release")
        await real_release(recorder)  # actually stop + deregister
        if release_cancels:  # finish-then-re-raise: recorder fully released, then the cancel surfaces
            raise asyncio.CancelledError()

    monkeypatch.setattr(factory_module, "release_display_recorder", _release)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("post-return-boom")

    monkeypatch.setattr(factory_module, "register_playwright_input_context", _boom)

    try:
        with pytest.raises(expected_exc):
            await BrowserContextFactory.create_browser_context(playwright=object())
        assert order == ["close", "release", "cleanup"]  # unmap -> fence release -> cleanup_func, in that order
        cleanup.assert_awaited_once()  # cleanup_func runs exactly once even when the release cancels
        assert (":99", "wr_t1") not in dr._REGISTRY and rec.is_stopped  # released + deregistered
    finally:
        dr._REGISTRY.pop((":99", "wr_t1"), None)
