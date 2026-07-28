from __future__ import annotations

import subprocess
import sys
from pathlib import Path

INSTALL_SCRIPT = Path(__file__).parents[2] / "prompt_evaluation" / "extract_action" / "scripts" / "install_directory.py"


def _install(source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(INSTALL_SCRIPT), str(source), str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_install_directory_moves_source_when_destination_is_absent(tmp_path: Path) -> None:
    source = tmp_path / "staged"
    source.mkdir()
    (source / "marker.txt").write_text("dataset", encoding="utf-8")
    destination = tmp_path / "dataset"

    result = _install(source, destination)

    assert result.returncode == 0, result.stderr
    assert not source.exists()
    assert (destination / "marker.txt").read_text(encoding="utf-8") == "dataset"


def test_install_directory_rejects_existing_empty_destination(tmp_path: Path) -> None:
    source = tmp_path / "staged"
    source.mkdir()
    destination = tmp_path / "dataset"
    destination.mkdir()

    result = _install(source, destination)

    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert source.is_dir()
    assert destination.is_dir()


def test_install_directory_rejects_existing_symlink(tmp_path: Path) -> None:
    source = tmp_path / "staged"
    source.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    destination = tmp_path / "dataset"
    destination.symlink_to(target, target_is_directory=True)

    result = _install(source, destination)

    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert source.is_dir()
    assert destination.is_symlink()
