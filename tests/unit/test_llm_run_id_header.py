import pytest

from skyvern.config import settings
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.schemas.llm import LLMConfig


def test_run_id_header_is_opt_in_and_prefers_workflow_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    llm_config = LLMConfig("openai/my-model", [], False, False)
    skyvern_context.set(SkyvernContext(task_id="tsk_1", workflow_run_id="wr_1"))
    try:
        monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_RUN_ID_HEADER", None)
        assert "extra_headers" not in LLMAPIHandlerFactory.get_api_parameters(llm_config)

        monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_RUN_ID_HEADER", "X-Run-Id")
        assert LLMAPIHandlerFactory.get_api_parameters(llm_config)["extra_headers"] == {"X-Run-Id": "wr_1"}

        skyvern_context.set(SkyvernContext(task_id="tsk_1"))
        assert LLMAPIHandlerFactory.get_api_parameters(llm_config)["extra_headers"] == {"X-Run-Id": "tsk_1"}
    finally:
        skyvern_context.reset()
