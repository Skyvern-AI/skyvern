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
async def test_ordinary_local_creator_leaves_marker_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory does not auto-stamp; a local creator's marker stays False."""
    from skyvern.webeye.browser_artifacts import BrowserArtifacts
    from skyvern.webeye.browser_factory import BrowserContextFactory

    async def _local_creator(playwright: Any, **kwargs: Any) -> tuple[Any, BrowserArtifacts, None]:
        return object(), BrowserArtifacts(), None

    monkeypatch.setattr(factory_module, "restore_session_cookies", AsyncMock())
    monkeypatch.setattr(factory_module, "set_browser_console_log", lambda **_: None)
    monkeypatch.setattr(factory_module, "set_popup_video_listener", lambda **_: None)
    monkeypatch.setattr(factory_module, "set_download_file_listener", lambda **_: None)
    monkeypatch.setattr(factory_module, "set_dialog_handler", lambda **_: None)

    class _FakeAgentFunction:
        async def setup_browser_context_extensions(self, **_: Any) -> None:
            return None

    class _FakeApp:
        AGENT_FUNCTION = _FakeAgentFunction()

    monkeypatch.setattr(factory_module, "app", _FakeApp())

    BrowserContextFactory.register_type("test-local", _local_creator)
    monkeypatch.setattr(factory_module.settings, "BROWSER_TYPE", "test-local")

    _, artifacts, _ = await BrowserContextFactory.create_browser_context(playwright=object())

    assert artifacts.needs_cdp_frame_publisher is False
