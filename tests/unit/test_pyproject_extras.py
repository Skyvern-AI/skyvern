from __future__ import annotations

from pathlib import Path
import tomllib


def _pyproject() -> dict:
    return tomllib.loads(Path("pyproject.toml").read_text())


def test_all_extra_stays_in_sync_with_server_plus_ui() -> None:
    optional_dependencies = _pyproject()["project"]["optional-dependencies"]

    assert sorted(optional_dependencies["all"]) == sorted(optional_dependencies["server"] + optional_dependencies["ui"])


def test_skyvern_ui_versions_match_root_package() -> None:
    root_version = _pyproject()["project"]["version"]
    ui_pyproject = tomllib.loads(Path("packages/skyvern-ui/pyproject.toml").read_text())
    ui_module = Path("packages/skyvern-ui/skyvern_ui/__init__.py").read_text()

    assert ui_pyproject["project"]["version"] == root_version
    assert f'__version__ = "{root_version}"' in ui_module
    assert _pyproject()["project"]["optional-dependencies"]["ui"] == [f"skyvern-ui=={root_version}"]
