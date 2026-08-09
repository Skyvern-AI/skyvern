"""``_connect_to_cdp_browser`` stamps ``needs_cdp_frame_publisher``.

It is the single chokepoint for remote-CDP creation here — ``cdp-connect``
always, plus ``chromium-headless`` / ``chromium-headful`` when
``browser_address`` is set — so one stamp there covers every remote-CDP path.
Ordinary local creators leave the marker False; the factory does not
auto-stamp.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye import browser_factory as factory_module


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
    from skyvern.webeye.browser_artifacts import BrowserArtifacts
    from skyvern.webeye.browser_factory import BrowserContextFactory

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


@pytest.mark.asyncio
async def test_factory_warns_when_requested_profile_not_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.webeye.browser_artifacts import BrowserArtifacts
    from skyvern.webeye.browser_factory import BrowserContextFactory

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
    from skyvern.webeye.browser_artifacts import BrowserArtifacts
    from skyvern.webeye.browser_factory import BrowserContextFactory

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
    from skyvern.webeye.browser_factory import BrowserContextFactory

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
    from skyvern.webeye.browser_factory import BrowserContextFactory

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
