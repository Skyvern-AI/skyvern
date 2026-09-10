from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from skyvern.forge.sdk.api.files import clean_up_dir
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.browser_factory import set_browser_console_log


class _FakeConsoleMessage:
    """Mirrors playwright's ConsoleMessage as set_browser_console_log reads it."""

    def __init__(self, text: str) -> None:
        self.type = "log"
        self.text = text
        self.location = {"url": "https://example.test/", "lineNumber": 1, "columnNumber": 1}


class _FakeBrowserContext:
    """Captures the console listener the way a real context's pyee emitter would."""

    def __init__(self) -> None:
        self.console_listener: Any = None

    def on(self, event: str, listener: Any) -> None:
        assert event == "console"
        self.console_listener = listener


def _register_console_listener(log_root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, BrowserArtifacts]:
    monkeypatch.setattr("skyvern.webeye.browser_factory.settings.LOG_PATH", str(log_root))
    browser_artifacts = BrowserArtifacts()
    context = _FakeBrowserContext()
    set_browser_console_log(context, browser_artifacts)  # type: ignore[arg-type]
    return context.console_listener, browser_artifacts


@pytest.mark.asyncio
async def test_console_listener_survives_a_wiped_log_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A browser context outlives its activity, whose teardown wipes the shared log root. Every
    console message the page emits after that must be dropped, not raised inside the pyee listener."""
    log_root = tmp_path / "log"
    listener, browser_artifacts = _register_console_listener(log_root, monkeypatch)
    log_path = browser_artifacts.browser_console_log_path
    assert log_path is not None

    await listener(_FakeConsoleMessage("before teardown"))
    assert "before teardown" in Path(log_path).read_text()

    clean_up_dir(str(log_root))
    assert not Path(log_path).exists()

    with structlog.testing.capture_logs() as logs:
        for i in range(50):
            assert await browser_artifacts.append_browser_console_log(f"after teardown {i}\n") == 0
            await listener(_FakeConsoleMessage(f"after teardown {i}"))

    unwritable = [r for r in logs if r.get("event", "").startswith("Browser console log is no longer writable")]
    assert len(unwritable) == 1, "the loss must be reported once per browser, not once per console message"
