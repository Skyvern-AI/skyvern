from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from skyvern.cli import doctor
from skyvern.cli.credential_placeholders import (
    CREDENTIAL_PLACEHOLDERS,
    is_frontend_api_key_placeholder,
    is_placeholder_credential_value,
)


class _StreamConnection:
    async def __aenter__(self) -> _StreamConnection:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def recv(self) -> str:
        return '{"status":"session_expired"}'


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


def test_llm_config_check_recognizes_openai_compatible_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_KEY", "OPENAI_COMPATIBLE")
    monkeypatch.setenv("ENABLE_OPENAI_COMPATIBLE", "true")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL_NAME", "some-vision-model")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_BASE", "https://example.com/v1")

    result = doctor._check_llm_config()

    assert result.status == "ok"
    assert "OPENAI_COMPATIBLE" in result.detail


def test_llm_config_check_flags_incomplete_openai_compatible_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_KEY", "OPENAI_COMPATIBLE")
    monkeypatch.setenv("ENABLE_OPENAI_COMPATIBLE", "true")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_COMPATIBLE_MODEL_NAME", raising=False)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_BASE", raising=False)

    result = doctor._check_llm_config()

    assert result.status == "error"
    assert "OPENAI_COMPATIBLE_MODEL_NAME" in result.detail
    assert "OPENAI_COMPATIBLE_API_BASE" in result.detail


def test_credential_placeholder_set_is_stable() -> None:
    assert CREDENTIAL_PLACEHOLDERS == ("", "PLACEHOLDER", "YOUR_API_KEY")


@pytest.mark.parametrize("model", [None, "", "GEMINI_3_PRO"])
def test_copilot_safety_check_reads_backend_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str | None
) -> None:
    env_path = tmp_path / "backend.env"
    if model is not None:
        env_path.write_text(f"WORKFLOW_COPILOT_LITE_LLM_KEY={model}\n")
    monkeypatch.setattr("skyvern.utils.env_paths.resolve_backend_env_path", lambda: env_path)
    monkeypatch.delenv("WORKFLOW_COPILOT_LITE_LLM_KEY", raising=False)
    monkeypatch.setenv("LLM_KEY", "GEMINI_3_PRO")
    monkeypatch.setenv("SECONDARY_LLM_KEY", "GEMINI_3_PRO")

    with patch.dict(os.environ):
        result = doctor._check_copilot_safety_config()

    assert doctor._check_copilot_safety_config in doctor._CHECKS
    if model:
        assert result.status == "ok"
        assert model in result.detail
    else:
        assert result.status == "warn"
        assert "PostHog override" in result.detail
        assert "WORKFLOW_COPILOT_LITE_LLM_KEY" in result.hint
        assert "recreate" in result.hint


def test_copilot_safety_check_honors_runtime_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / "backend.env"
    env_path.write_text("WORKFLOW_COPILOT_LITE_LLM_KEY=GEMINI_3_PRO\n")
    monkeypatch.setattr("skyvern.utils.env_paths.resolve_backend_env_path", lambda: env_path)
    monkeypatch.setenv("WORKFLOW_COPILOT_LITE_LLM_KEY", "OPENAI_GPT5_4")

    result = doctor._check_copilot_safety_config()

    assert result.status == "ok"
    assert "OPENAI_GPT5_4" in result.detail


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


@pytest.mark.asyncio
async def test_stream_doctor_stops_when_browser_session_has_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("websockets.connect", lambda *args, **kwargs: _StreamConnection())

    with pytest.raises(RuntimeError, match="stream ended before a frame arrived: session_expired"):
        await doctor._wait_for_stream_frame("ws://example.test", timeout_seconds=1)
