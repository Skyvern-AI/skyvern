from __future__ import annotations

from pathlib import Path

import pytest

from skyvern.cli import doctor


def _prepare_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    frontend = tmp_path / "skyvern-frontend"
    frontend.mkdir()
    (frontend / ".env.example").write_text("VITE_SKYVERN_API_KEY=YOUR_API_KEY\n")
    return tmp_path


def _write_legacy_secret(tmp_path: Path, body: str) -> Path:
    legacy = tmp_path / ".streamlit" / "secrets.toml"
    legacy.parent.mkdir()
    legacy.write_text(body)
    return legacy


def test_legacy_streamlit_check_is_ok_when_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_workspace(tmp_path, monkeypatch)

    result = doctor._check_legacy_streamlit_secrets()

    assert result.status == "ok"
    assert result.detail == "not present"


def test_legacy_streamlit_fix_preserves_unparseable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    legacy = _write_legacy_secret(tmp_path, '[general]\nnot_cred = "keep-me"\n')

    result = doctor._check_legacy_streamlit_secrets()

    assert result.status == "warn"
    assert "no cred value" in result.detail
    assert doctor._fix_legacy_streamlit_secrets() is False
    assert legacy.exists()


def test_legacy_streamlit_fix_migrates_only_parseable_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    legacy = _write_legacy_secret(tmp_path, '[general]\ncred = "legacy-key"\n')

    result = doctor._check_legacy_streamlit_secrets()

    assert result.status == "warn"
    assert "backend .env is missing" in result.detail
    assert doctor._fix_legacy_streamlit_secrets() is True
    assert not legacy.exists()
    assert "SKYVERN_API_KEY='legacy-key'" in (tmp_path / ".env").read_text()
    assert "VITE_SKYVERN_API_KEY='legacy-key'" in (tmp_path / "skyvern-frontend" / ".env").read_text()


def test_legacy_streamlit_fix_removes_matching_deprecated_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    legacy = _write_legacy_secret(tmp_path, '[general]\ncred = "same-key"\n')
    (tmp_path / ".env").write_text('SKYVERN_API_KEY="same-key"\n')

    result = doctor._check_legacy_streamlit_secrets()

    assert result.status == "warn"
    assert "deprecated compatibility file" in result.detail
    assert doctor._fix_legacy_streamlit_secrets() is True
    assert not legacy.exists()
