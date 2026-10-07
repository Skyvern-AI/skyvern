import copy

import litellm
import pytest
from litellm.utils import _invalidate_model_cost_lowercase_map

from tests.unit.litellm_model_registry import (
    GPT56_LITELLM_MODELS,
    RESTORED_PROVIDERS,
    registered_gpt56_litellm_models,
)


def test_gpt56_models_use_only_providers_whose_registry_is_restored() -> None:
    assert {entry["litellm_provider"] for entry in GPT56_LITELLM_MODELS.values()} <= RESTORED_PROVIDERS


def test_registered_gpt56_models_resolve_inside_and_leave_no_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    # Start from a registry without GPT-5.6 so the result does not depend on what earlier tests registered.
    with monkeypatch.context() as patch:
        patch.setattr(
            litellm,
            "model_cost",
            {key: copy.deepcopy(value) for key, value in litellm.model_cost.items() if key not in GPT56_LITELLM_MODELS},
        )
        patch.setattr(
            litellm,
            "open_ai_chat_completion_models",
            set(litellm.open_ai_chat_completion_models) - GPT56_LITELLM_MODELS.keys(),
        )
        _invalidate_model_cost_lowercase_map()
        model_cost_before = copy.deepcopy(litellm.model_cost)
        openai_models_before = set(litellm.open_ai_chat_completion_models)

        with registered_gpt56_litellm_models():
            for model, entry in GPT56_LITELLM_MODELS.items():
                assert litellm.get_llm_provider(model)[1] == entry["litellm_provider"]
                assert litellm.get_model_info(model)["mode"] == "chat"

        assert litellm.model_cost == model_cost_before
        assert litellm.open_ai_chat_completion_models == openai_models_before
        with pytest.raises(litellm.exceptions.BadRequestError):
            litellm.get_llm_provider("gpt-5.6-sol")
        with pytest.raises(Exception, match="isn't mapped"):
            litellm.get_model_info("gpt-5.6-sol")
    _invalidate_model_cost_lowercase_map()
