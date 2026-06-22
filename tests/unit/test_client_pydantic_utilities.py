from __future__ import annotations

import pytest

from skyvern.client.core.pydantic_utilities import IS_PYDANTIC_V2, update_forward_refs


@pytest.mark.skipif(not IS_PYDANTIC_V2, reason="Pydantic v2-specific generated-client rebuild behavior")
def test_update_forward_refs_suppresses_pydantic_v2_definitions_key_error() -> None:
    class GeneratedModel:
        @classmethod
        def model_rebuild(cls, *, raise_errors: bool) -> None:
            assert raise_errors is False
            raise KeyError("definitions")

    update_forward_refs(GeneratedModel)  # type: ignore[type-var]


@pytest.mark.skipif(not IS_PYDANTIC_V2, reason="Pydantic v2-specific generated-client rebuild behavior")
def test_update_forward_refs_suppresses_definitions_key_error_with_extra_context() -> None:
    class GeneratedModel:
        @classmethod
        def model_rebuild(cls, *, raise_errors: bool) -> None:
            assert raise_errors is False
            raise KeyError("definitions", "extra-context")

    update_forward_refs(GeneratedModel)  # type: ignore[type-var]


@pytest.mark.skipif(not IS_PYDANTIC_V2, reason="Pydantic v2-specific generated-client rebuild behavior")
def test_update_forward_refs_preserves_unexpected_key_error() -> None:
    class GeneratedModel:
        @classmethod
        def model_rebuild(cls, *, raise_errors: bool) -> None:
            assert raise_errors is False
            raise KeyError("other")

    with pytest.raises(KeyError, match="other"):
        update_forward_refs(GeneratedModel)  # type: ignore[type-var]
