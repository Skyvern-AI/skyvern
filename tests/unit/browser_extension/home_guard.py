import os
import pwd
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _test_broker_base_dir() -> Path:
    return Path(os.environ["SKYVERN_BROWSER_EXTENSION_TEST_BASE_DIR"])


def _assert_outside_real_home(path: Path, source: str) -> Path:
    resolved = path.resolve()
    if resolved == _REAL_HOME or resolved.is_relative_to(_REAL_HOME):
        pytest.fail(f"{source} resolved beneath the real home: {resolved}")
    return path


@pytest.fixture(autouse=True)
def isolate_browser_extension_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep broker state and default home-derived paths out of the operator's home."""
    test_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(test_home))

    def isolated_home(_cls: type[Path]) -> Path:
        return _assert_outside_real_home(
            Path(os.environ.get("HOME", str(test_home))),
            "Path.home()",
        )

    original_expanduser = os.path.expanduser

    def isolated_expanduser(path: str) -> str:
        if path == "~":
            expanded = str(isolated_home(Path))
        elif path.startswith("~/"):
            expanded = str(isolated_home(Path) / path[2:])
        else:
            expanded = original_expanduser(path)
        if path.startswith("~"):
            _assert_outside_real_home(Path(expanded), "os.path.expanduser()")
        return expanded

    monkeypatch.setattr(Path, "home", classmethod(isolated_home))
    monkeypatch.setattr(os.path, "expanduser", isolated_expanduser)
    with tempfile.TemporaryDirectory(prefix="skyvern-browser-extension-", dir="/tmp") as base_dir:
        monkeypatch.setenv("SKYVERN_BROWSER_EXTENSION_TEST_BASE_DIR", base_dir)
        yield
