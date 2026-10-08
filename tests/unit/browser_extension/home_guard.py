import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

if os.name == "nt":
    _REAL_HOME = Path.home().resolve()
else:
    import pwd

    _REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _test_broker_base_dir() -> Path:
    return Path(os.environ["SKYVERN_BROWSER_EXTENSION_TEST_BASE_DIR"])


def _assert_isolated_path(path: Path, source: str, test_root: Path) -> Path:
    resolved = path.resolve()
    # Windows commonly puts pytest's temporary directory beneath USERPROFILE.
    if resolved.is_relative_to(_REAL_HOME) and not resolved.is_relative_to(test_root.resolve()):
        pytest.fail(f"{source} resolved beneath the real home: {resolved}")
    return path


@pytest.fixture(autouse=True)
def isolate_browser_extension_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate broker state and home-derived paths in temporary test directories."""
    test_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(test_home))
    monkeypatch.setenv("USERPROFILE", str(test_home))

    def isolated_home(_cls: type[Path]) -> Path:
        return _assert_isolated_path(
            Path(os.environ.get("HOME", str(test_home))),
            "Path.home()",
            tmp_path,
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
            _assert_isolated_path(Path(expanded), "os.path.expanduser()", tmp_path)
        return expanded

    monkeypatch.setattr(Path, "home", classmethod(isolated_home))
    monkeypatch.setattr(os.path, "expanduser", isolated_expanduser)
    with tempfile.TemporaryDirectory(
        prefix="skyvern-browser-extension-", dir=None if os.name == "nt" else "/tmp"
    ) as base_dir:
        monkeypatch.setenv("SKYVERN_BROWSER_EXTENSION_TEST_BASE_DIR", base_dir)
        yield
