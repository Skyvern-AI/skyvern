from unittest.mock import MagicMock

from skyvern.forge.sdk.api.llm.api_handler_factory import (
    _llm_screenshots_enabled_metric,
    _llm_screenshots_for_call,
)
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.schemas.llm import LLMConfig


def test_screenshots_attached_when_vision_and_flag_off() -> None:
    config = MagicMock(spec=LLMConfig)
    config.supports_vision = True
    ctx = SkyvernContext(disable_llm_screenshots=False)
    assert _llm_screenshots_for_call([b"png"], config, ctx) == [b"png"]
    assert _llm_screenshots_enabled_metric(config, ctx) is True


def test_screenshots_stripped_when_flag_on() -> None:
    config = MagicMock(spec=LLMConfig)
    config.supports_vision = True
    ctx = SkyvernContext(disable_llm_screenshots=True)
    assert _llm_screenshots_for_call([b"png"], config, ctx) is None
    assert _llm_screenshots_enabled_metric(config, ctx) is False


def test_non_vision_model_never_attaches() -> None:
    config = MagicMock(spec=LLMConfig)
    config.supports_vision = False
    ctx = SkyvernContext(disable_llm_screenshots=False)
    assert _llm_screenshots_for_call([b"png"], config, ctx) is None
    assert _llm_screenshots_enabled_metric(config, ctx) is False


def test_none_context_treated_as_screenshots_enabled_for_vision() -> None:
    config = MagicMock(spec=LLMConfig)
    config.supports_vision = True
    assert _llm_screenshots_for_call([b"png"], config, None) == [b"png"]
    assert _llm_screenshots_enabled_metric(config, None) is True


def test_none_screenshots_input_still_reports_cohort_when_vision() -> None:
    config = MagicMock(spec=LLMConfig)
    config.supports_vision = True
    ctx = SkyvernContext(disable_llm_screenshots=False)
    assert _llm_screenshots_for_call(None, config, ctx) is None
    assert _llm_screenshots_enabled_metric(config, ctx) is True
