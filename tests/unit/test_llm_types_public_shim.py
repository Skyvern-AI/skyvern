import copy
import importlib
import pickle

import pytest

from skyvern.schemas.llm import LLMConfig as CanonicalLLMConfig
from skyvern.schemas.llm import LLMRouterConfig as CanonicalLLMRouterConfig


def test_forge_llm_models_shim_warns_and_preserves_class_identity() -> None:
    models = importlib.import_module("skyvern.forge.sdk.api.llm.models")

    with pytest.warns(DeprecationWarning, match="skyvern.schemas.llm"):
        legacy_llm_config = models.LLMConfig

    with pytest.warns(DeprecationWarning, match="skyvern.schemas.llm"):
        legacy_router_config = models.LLMRouterConfig

    assert legacy_llm_config is CanonicalLLMConfig
    assert legacy_router_config is CanonicalLLMRouterConfig


def test_llm_config_defaults_resolve_at_construction() -> None:
    config = CanonicalLLMConfig(
        model_name="gpt-test",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )

    assert config.max_tokens is not None
    assert config.temperature is not None


def test_llm_config_preserves_explicit_none_generation_params() -> None:
    config = CanonicalLLMConfig(
        model_name="o-series-test",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        max_tokens=None,
        temperature=None,
    )

    assert config.max_tokens is None
    assert config.temperature is None


def test_llm_router_config_preserves_explicit_none_generation_params() -> None:
    config = CanonicalLLMRouterConfig(
        model_name="router-test",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        model_list=[],
        main_model_group="router-test",
        max_tokens=None,
        temperature=None,
    )

    assert config.max_tokens is None
    assert config.temperature is None


def test_llm_config_generation_defaults_survive_copy_and_pickle() -> None:
    configs: list[CanonicalLLMConfig | CanonicalLLMRouterConfig] = [
        CanonicalLLMConfig(
            model_name="gpt-test",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
        ),
        CanonicalLLMRouterConfig(
            model_name="router-test",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            model_list=[],
            main_model_group="router-test",
        ),
    ]

    for config in configs:
        for clone in (copy.deepcopy(config), pickle.loads(pickle.dumps(config))):
            assert clone.max_tokens == config.max_tokens
            assert clone.temperature == config.temperature


def test_cloud_router_config_instances_keep_public_type_identity() -> None:
    from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry

    llm_key = "TEST_PUBLIC_TYPE_IDENTITY"
    LLMConfigRegistry.deregister_config(llm_key)
    LLMConfigRegistry.register_config(
        llm_key,
        CanonicalLLMConfig(
            model_name="gpt-test",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
        ),
    )

    try:
        config = LLMConfigRegistry.get_config(llm_key)
        assert isinstance(config, CanonicalLLMConfig)
    finally:
        LLMConfigRegistry.deregister_config(llm_key)
