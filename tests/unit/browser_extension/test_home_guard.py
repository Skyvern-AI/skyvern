from pathlib import Path

import pytest

from tests.unit.browser_extension import home_guard


@pytest.mark.parametrize("relative_path", ["", ".skyvern", "AppData/Local/Temp/unrelated"])
def test_home_guard_rejects_operator_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_path: str) -> None:
    real_home = tmp_path / "operator"
    monkeypatch.setattr(home_guard, "_REAL_HOME", real_home)
    test_root = real_home / "AppData/Local/Temp/pytest/sandbox"

    with pytest.raises(pytest.fail.Exception, match="resolved beneath the real home"):
        home_guard._assert_isolated_path(real_home / relative_path, "test", test_root)


def test_home_guard_allows_pytest_sandbox_beneath_operator_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_home = tmp_path / "operator"
    monkeypatch.setattr(home_guard, "_REAL_HOME", real_home)
    test_root = real_home / "AppData/Local/Temp/pytest/sandbox"
    test_home = test_root / "home"

    assert home_guard._assert_isolated_path(test_home, "test", test_root) == test_home
