from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dotenv import dotenv_values

from skyvern.cli import llm_setup


@pytest.mark.parametrize(
    ("existing_model", "selected_model", "runtime_model"),
    [
        (None, None, None),
        ("", None, None),
        ("GEMINI_2.5_FLASH", None, None),
        ("CUSTOM_SAFETY_MODEL", None, None),
        ("GEMINI_2.5_FLASH", "GEMINI_3.1_PRO", None),
        (None, "GEMINI_2.5_FLASH", None),
        (None, None, "CUSTOM_RUNTIME_MODEL"),
        ("CUSTOM_FILE_MODEL", None, "CUSTOM_RUNTIME_MODEL"),
        ("CUSTOM_FILE_MODEL", None, ""),
    ],
)
def test_setup_explicitly_configures_copilot_safety_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_model: str | None,
    selected_model: str | None,
    runtime_model: str | None,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKFLOW_COPILOT_LITE_LLM_KEY", raising=False)
    if runtime_model is not None:
        monkeypatch.setenv("WORKFLOW_COPILOT_LITE_LLM_KEY", runtime_model)
    env_path = tmp_path / "config" / "backend.env"
    if existing_model is not None:
        env_path.parent.mkdir()
        env_path.write_text(f"WORKFLOW_COPILOT_LITE_LLM_KEY='{existing_model}'\n")

    confirmations = iter([False, False, False, False, True, False, False])
    monkeypatch.setattr(llm_setup.Confirm, "ask", lambda *_args, **_kwargs: next(confirmations))
    monkeypatch.setattr(llm_setup, "ask_secret", lambda *_args, **_kwargs: "test-gemini-key")
    monkeypatch.setattr(llm_setup, "capture_setup_event", MagicMock())
    monkeypatch.setattr(llm_setup, "console", MagicMock())
    prompts: list[dict[str, object]] = []

    def ask_model(prompt: str, *, choices: list[str], default: str) -> str:
        if "Copilot safety" in prompt:
            prompts.append({"choices": choices, "default": default})
            return selected_model or default
        return "1"

    monkeypatch.setattr(llm_setup.Prompt, "ask", ask_model)
    with patch.dict(os.environ):
        llm_setup.setup_llm_providers(env_path=env_path)

    expected_default = existing_model or runtime_model or "GEMINI_3.1_PRO"
    values = dotenv_values(env_path)
    assert values["LLM_KEY"] == "GEMINI_3.1_PRO"
    assert values["WORKFLOW_COPILOT_LITE_LLM_KEY"] == (selected_model or expected_default)
    assert values["GEMINI_API_KEY"] == "test-gemini-key"
    assert values["ENABLE_YUTORI"] == "false"
    assert len(prompts) == 1
    assert prompts[0]["default"] == expected_default
    assert expected_default in prompts[0]["choices"]
    assert not (tmp_path / ".env").exists()


def test_setup_without_provider_does_not_configure_safety_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / "backend.env"
    monkeypatch.setattr(llm_setup.Confirm, "ask", lambda *_args, **_kwargs: False)
    prompt = MagicMock()
    monkeypatch.setattr(llm_setup.Prompt, "ask", prompt)
    monkeypatch.setattr(llm_setup, "capture_setup_event", MagicMock())
    monkeypatch.setattr(llm_setup, "console", MagicMock())

    with patch.dict(os.environ):
        llm_setup.setup_llm_providers(env_path=env_path)

    prompt.assert_not_called()
    assert dotenv_values(env_path)["WORKFLOW_COPILOT_LITE_LLM_KEY"] == ""
