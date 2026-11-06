from pathlib import Path

import pytest

from tests.unit.chromium_probe import chromium_available


def test_a_missing_chromium_reads_as_not_installed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The probe tells "not installed" from "installed but broken" by Playwright's launch error text. If an upgrade
    # rewords it, every shard without Chromium would run its browser tests instead of skipping them.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert chromium_available.__wrapped__() is False
