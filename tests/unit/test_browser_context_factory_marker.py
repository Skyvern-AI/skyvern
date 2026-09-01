"""``_connect_to_cdp_browser`` stamps ``needs_cdp_frame_publisher``.

It is the single chokepoint for remote-CDP creation here — ``cdp-connect``
always, plus ``chromium-headless`` / ``chromium-headful`` when
``browser_address`` is set — so one stamp there covers every remote-CDP path.
Ordinary local creators leave the marker False; the factory does not
auto-stamp.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import BrowserContext, Locator, Page

from skyvern.forge.sdk.workflow.models.code_block_recorder import RecordingPage
from skyvern.webeye import browser_factory as factory_module
from skyvern.webeye import display_recorder as recorder_module
from skyvern.webeye.browser_artifacts import BrowserArtifacts
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


def _register_eligible_local_creator(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from skyvern.webeye.browser_artifacts import BrowserArtifacts
    from skyvern.webeye.browser_factory import BrowserContextFactory

    async def _eligible_creator(playwright: Any, **kwargs: Any) -> tuple[Any, BrowserArtifacts, None]:
        artifacts = BrowserArtifacts()
        # Mirrors what the real local-launch creators do after configure_local_display_recording().
        artifacts.local_display_recording_eligible = True
        return object(), artifacts, None

    BrowserContextFactory.register_type(name, _eligible_creator)
    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", name)


@pytest.mark.asyncio
async def test_eligible_local_launch_seeds_single_artifact_and_skips_playwright_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from skyvern.webeye.browser_artifacts import VideoArtifact
    from skyvern.webeye.browser_factory import BrowserContextFactory
    from skyvern.webeye.display_recorder import DisplayRecorderAcquisition

    _factory_harness(monkeypatch)
    popup = MagicMock()
    monkeypatch.setattr(factory_module, "set_popup_video_listener", popup)
    seeded = VideoArtifact(video_path=str(tmp_path / "wr_seed.webm"))
    recorder = MagicMock()
    acquire = AsyncMock(return_value=DisplayRecorderAcquisition(recorder, seeded, True))
    monkeypatch.setattr(factory_module, "acquire_display_recorder", acquire)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(factory_module.settings, "VIDEO_PATH", str(tmp_path))
    _register_eligible_local_creator(monkeypatch, "test-eligible-seed")

    _, artifacts, _ = await BrowserContextFactory.create_browser_context(playwright=object(), workflow_run_id="wr_seed")

    assert acquire.await_count == 1
    # Exactly one run-scoped VideoArtifact, seeded synchronously in the creator tail.
    assert artifacts.video_artifacts == [seeded]
    assert artifacts._display_recorder is recorder
    # The Playwright per-page listener must NOT run on the eligible whole-display path.
    popup.assert_not_called()


@pytest.mark.asyncio
async def test_remote_cdp_path_never_spawns_recorder_and_keeps_playwright_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from skyvern.webeye.browser_artifacts import BrowserArtifacts
    from skyvern.webeye.browser_factory import BrowserContextFactory

    _factory_harness(monkeypatch)
    popup = MagicMock()
    monkeypatch.setattr(factory_module, "set_popup_video_listener", popup)
    acquire = AsyncMock()
    monkeypatch.setattr(factory_module, "acquire_display_recorder", acquire)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(factory_module.settings, "VIDEO_PATH", str(tmp_path))

    async def _remote_creator(playwright: Any, **kwargs: Any) -> tuple[Any, BrowserArtifacts, None]:
        # Remote/CDP/vendor creators never set the eligibility marker.
        return object(), BrowserArtifacts(), None

    BrowserContextFactory.register_type("test-remote", _remote_creator)
    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", "test-remote")

    _, artifacts, _ = await BrowserContextFactory.create_browser_context(playwright=object(), workflow_run_id="wr_x")

    assert acquire.await_count == 0
    assert artifacts.video_artifacts == []
    assert artifacts._display_recorder is None
    popup.assert_called_once()


@pytest.mark.asyncio
async def test_same_owner_recreation_reuses_exact_video_artifact_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Recreation (check_and_fix_state re-enters the factory) must reuse the exact VideoArtifact object so
    its video_artifact_id survives, which is what keeps agent.initialize_execution_state at one RECORDING row."""
    from skyvern.webeye.browser_factory import BrowserContextFactory

    _factory_harness(monkeypatch)
    recorder_module._REGISTRY.clear()
    process = MagicMock(returncode=None)
    process.wait = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(factory_module.settings, "VIDEO_PATH", str(tmp_path))
    _register_eligible_local_creator(monkeypatch, "test-eligible-reuse")

    try:
        _, first, _ = await BrowserContextFactory.create_browser_context(
            playwright=object(), workflow_run_id="wr_reuse"
        )
        _, second, _ = await BrowserContextFactory.create_browser_context(
            playwright=object(), workflow_run_id="wr_reuse"
        )

        assert first.video_artifacts[0] is second.video_artifacts[0]
        # Step-1 stamps the id on the shared object; recreation therefore never yields a second row.
        first.video_artifacts[0].video_artifact_id = "va_1"
        assert second.video_artifacts[0].video_artifact_id == "va_1"
    finally:
        process.returncode = 0
        await recorder_module.release_display_recorder(first._display_recorder)
        recorder_module._REGISTRY.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("producer_kwargs", "reconnect_kwargs"),
    [
        pytest.param({"task_id": "tsk_solo"}, {}, id="standalone"),
        pytest.param({"workflow_run_id": "wr_parent"}, {"workflow_run_id": "wr_child"}, id="aliased-child"),
    ],
)
async def test_reconnect_end_to_end_preserves_exact_display_recorder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, producer_kwargs: dict[str, str], reconnect_kwargs: dict[str, str]
) -> None:
    """End-to-end (real factory + reconnect()): the rebuild must re-acquire the EXACT same recorder + index-0
    VideoArtifact, popup suppressed. Covers standalone reconnect AND the use_parent_browser_session aliased
    child (parent-owned recorder, child reconnect id) — RED without the acquire-level override reuse."""
    from skyvern.webeye.browser_factory import BrowserContextFactory
    from skyvern.webeye.real_browser_state import RealBrowserState

    _factory_harness(monkeypatch)
    recorder_module._REGISTRY.clear()
    popup = MagicMock()
    monkeypatch.setattr(factory_module, "set_popup_video_listener", popup)
    process = MagicMock(returncode=None, wait=AsyncMock())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(factory_module.settings, "VIDEO_PATH", str(tmp_path))
    _register_eligible_local_creator(monkeypatch, "test-eligible-e2e")

    _, first, _ = await BrowserContextFactory.create_browser_context(playwright=object(), **producer_kwargs)
    recorder0, artifact0 = first._display_recorder, first.video_artifacts[0]
    assert recorder0 is not None

    state = RealBrowserState.__new__(RealBrowserState)
    state.pw = AsyncMock()
    state.browser_context = MagicMock()
    state.engine_selection = MagicMock(start_driver=AsyncMock(return_value=AsyncMock()))
    state.set_working_page = AsyncMock()
    state.get_working_page = AsyncMock(return_value=MagicMock())
    state.browser_artifacts = first

    try:
        await state.reconnect(**reconnect_kwargs)
        assert state.browser_artifacts._display_recorder is recorder0, "exact same recorder survives reconnect"
        assert state.browser_artifacts.video_artifacts == [artifact0], "one index-0 VideoArtifact object survives"
        popup.assert_not_called()
    finally:
        process.returncode = 0
        await recorder_module.release_display_recorder(recorder0)
        recorder_module._REGISTRY.clear()


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
