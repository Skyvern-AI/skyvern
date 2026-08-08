from __future__ import annotations

from pathlib import Path

import pytest

from skyvern.cli import doctor
from skyvern.cli.credential_placeholders import (
    CREDENTIAL_PLACEHOLDERS,
    is_frontend_api_key_placeholder,
    is_placeholder_credential_value,
)


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
    assert "SKYVERN_API_KEY=legacy-key" in (tmp_path / ".env").read_text()
    assert "VITE_SKYVERN_API_KEY=legacy-key" in (tmp_path / "skyvern-frontend" / ".env").read_text()


def test_legacy_streamlit_fix_removes_matching_deprecated_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    legacy = _write_legacy_secret(tmp_path, '[general]\ncred = "same-key"\n')
    (tmp_path / ".env").write_text('SKYVERN_API_KEY="same-key"\n')

    result = doctor._check_legacy_streamlit_secrets()

    assert result.status == "warn"
    assert "deprecated compatibility file" in result.detail
    assert doctor._fix_legacy_streamlit_secrets() is True
    assert not legacy.exists()


def test_credential_placeholder_set_is_stable() -> None:
    assert CREDENTIAL_PLACEHOLDERS == ("", "PLACEHOLDER", "YOUR_API_KEY")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", True),
        ("PLACEHOLDER", True),
        ("YOUR_API_KEY", True),
        ("__SKYVERN_API_KEY_PLACEHOLDER__", True),
        ("__VITE_API_BASE_URL_PLACEHOLDER__", True),
        ("real-value", False),
    ],
)
def test_placeholder_credential_value_classification(value: str, expected: bool) -> None:
    assert is_placeholder_credential_value(value) is expected


def test_api_key_consistency_treats_frontend_api_key_sentinel_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text('SKYVERN_API_KEY="backend-key"\n')
    (tmp_path / "skyvern-frontend" / ".env").write_text("VITE_SKYVERN_API_KEY=YOUR_API_KEY\n")

    result = doctor._check_api_key_consistency()

    assert result.status == "error"
    assert result.detail == "VITE_SKYVERN_API_KEY not set in frontend .env"


def test_api_key_consistency_treats_frontend_api_key_SENTINEL_PLACEHOLDER_as_value_to_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text('SKYVERN_API_KEY="backend-key"\n')
    (tmp_path / "skyvern-frontend" / ".env").write_text("VITE_SKYVERN_API_KEY=PLACEHOLDER\n")

    result = doctor._check_api_key_consistency()

    assert result.status == "error"
    assert "frontend .env differs from backend" in result.detail


def test_frontend_api_key_placeholder_only_filter_is_consistent() -> None:
    assert is_frontend_api_key_placeholder("YOUR_API_KEY")
    assert is_frontend_api_key_placeholder("")
    assert is_frontend_api_key_placeholder("PLACEHOLDER") is False
