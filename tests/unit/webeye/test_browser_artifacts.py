import shutil
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


def _events(logs: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    return [record for record in logs if record.get("event", "").startswith(prefix)]


@pytest.mark.asyncio
async def test_console_listener_recreates_a_wiped_log_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activity teardown and the aged-dir stale sweep both remove the per-day dir under the shared log root
    while a browser context that outlives its activity still holds a path there. Console output emitted after
    the wipe must land in a recreated log — not raise inside the pyee listener, not be dropped — and the wipe
    is reported once per browser, not once per message."""
    log_root = tmp_path / "log"
    listener, browser_artifacts = _register_console_listener(log_root, monkeypatch)
    log_path = browser_artifacts.browser_console_log_path
    assert log_path is not None

    await listener(_FakeConsoleMessage("before teardown"))
    assert "before teardown" in Path(log_path).read_text()

    clean_up_dir(str(log_root))
    assert not Path(log_path).parent.exists()

    with structlog.testing.capture_logs() as logs:
        for i in range(50):
            await listener(_FakeConsoleMessage(f"after teardown {i}"))

    lines = (await browser_artifacts.read_browser_console_log()).decode().splitlines()
    retained = {line.split("[log]", 1)[1].split(" url=", 1)[0] for line in lines if "[log]" in line}
    expected = {f"after teardown {i}" for i in range(50)}
    assert expected <= retained, f"console output emitted after the wipe was lost: {sorted(expected - retained)}"
    assert len(lines) == 50, "each post-wipe message must land exactly once"
    assert len(_events(logs, "Browser console log directory was wiped")) == 1
    assert not _events(logs, "Browser console log is no longer writable")


@pytest.mark.asyncio
async def test_console_listener_drops_output_when_the_log_directory_cannot_be_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the wiped directory cannot be re-made, the listener still must not raise: the output is dropped
    and the loss is reported once per browser, not once per console message."""
    log_root = tmp_path / "log"
    listener, browser_artifacts = _register_console_listener(log_root, monkeypatch)

    shutil.rmtree(log_root)
    log_root.write_text("a regular file now squats on the log root")

    with structlog.testing.capture_logs() as logs:
        for i in range(50):
            await listener(_FakeConsoleMessage(f"after teardown {i}"))

    assert await browser_artifacts.read_browser_console_log() == b""
    assert len(_events(logs, "Browser console log is no longer writable")) == 1
