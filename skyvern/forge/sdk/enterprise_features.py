from typing import Any

from skyvern.schemas.runs import RunEngine

ENTERPRISE_GATED_RUN_ENGINES: dict[RunEngine, str] = {
    RunEngine.openai_cua: "OpenAI CUA",
    RunEngine.anthropic_cua: "Anthropic CUA",
}
ENTERPRISE_GATED_MODEL_NAMES: dict[str, str] = {
    "us.anthropic.claude-opus-4-20250514-v1:0": "Anthropic Claude 4 Opus",
    "claude-opus-4-5-20251101": "Anthropic Claude 4.5 Opus",
    "claude-opus-4-6": "Anthropic Claude 4.6 Opus",
    "claude-opus-4-7": "Anthropic Claude 4.7 Opus",
    "claude-opus-4-8": "Anthropic Claude 4.8 Opus",
    "claude-fable-5": "Anthropic Claude Fable 5",
}


def _get_model_name(model: dict[str, Any] | None) -> str | None:
    if not isinstance(model, dict):
        return None
    model_name = model.get("model_name")
    return model_name if isinstance(model_name, str) else None


def collect_enterprise_gated_run_features(
    *,
    engine: RunEngine | None = None,
    model: dict[str, Any] | None = None,
) -> set[str]:
    feature_names: set[str] = set()

    if engine in ENTERPRISE_GATED_RUN_ENGINES:
        feature_names.add(ENTERPRISE_GATED_RUN_ENGINES[engine])

    model_name = _get_model_name(model)
    if model_name in ENTERPRISE_GATED_MODEL_NAMES:
        feature_names.add(ENTERPRISE_GATED_MODEL_NAMES[model_name])

    return feature_names
