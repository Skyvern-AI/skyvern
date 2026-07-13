from __future__ import annotations

from skyvern.config import settings


def test_configured_openai_compatible_model_is_selectable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_OPENAI_COMPATIBLE", True)
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MODEL_NAME", "tokenless-pro")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_ADDITIONAL_MODEL_NAMES", " tokenless-ultra-saver,tokenless-pro ")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MODEL_KEY", "OPENAI_COMPATIBLE")

    mapping = settings.get_model_name_to_llm_key()

    assert mapping["tokenless-pro"] == {
        "llm_key": "OPENAI_COMPATIBLE",
        "label": "OpenAI-compatible: tokenless-pro",
    }
    assert mapping["tokenless-ultra-saver"] == {
        "llm_key": "OPENAI_COMPATIBLE",
        "label": "OpenAI-compatible: tokenless-ultra-saver",
    }
    assert settings.get_openai_compatible_model_names() == ["tokenless-pro", "tokenless-ultra-saver"]
