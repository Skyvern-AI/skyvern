import copy
from collections.abc import Iterator
from contextlib import contextmanager

import litellm
from litellm.utils import _invalidate_model_cost_lowercase_map

# LiteLLM's bundled cost map (LITELLM_LOCAL_MODEL_COST_MAP=true in CI) predates GPT-5.6, so tests that
# route GPT-5.6 models must register them rather than rely on another test having done so.
_GPT56_CAPABILITIES = {
    "mode": "chat",
    "supports_function_calling": True,
    "supports_prompt_caching": True,
    "supports_reasoning": True,
}
# register_model also adds openai keys to open_ai_chat_completion_models; azure keys touch only model_cost.
# Those are the only collections restored below, so these are the only providers allowed here.
RESTORED_PROVIDERS = frozenset({"openai", "azure"})
GPT56_LITELLM_MODELS: dict[str, dict[str, object]] = {
    **{f"gpt-5.6-{variant}": {"litellm_provider": "openai", **_GPT56_CAPABILITIES} for variant in ("sol", "terra")},
    **{
        f"azure/gpt-5.6-{variant}": {"litellm_provider": "azure", **_GPT56_CAPABILITIES} for variant in ("sol", "terra")
    },
}


@contextmanager
def registered_gpt56_litellm_models() -> Iterator[None]:
    # Restore in place rather than rebinding, so references taken before entry keep seeing the original state.
    saved_model_cost = copy.deepcopy(litellm.model_cost)
    saved_openai_models = set(litellm.open_ai_chat_completion_models)
    try:
        litellm.register_model(copy.deepcopy(GPT56_LITELLM_MODELS))
        yield
    finally:
        litellm.model_cost.clear()
        litellm.model_cost.update(saved_model_cost)
        litellm.open_ai_chat_completion_models.clear()
        litellm.open_ai_chat_completion_models.update(saved_openai_models)
        _invalidate_model_cost_lowercase_map()
