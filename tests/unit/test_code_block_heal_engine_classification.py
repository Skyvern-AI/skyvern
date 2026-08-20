from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError

from skyvern.forge import app
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.schemas.self_heal import HealClassification, HealSkipReason
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection


class _NativeError(Exception):
    pass


class _NativeTimeoutError(_NativeError):
    pass


class _ForeignError(Exception):
    pass


class _ForeignTimeoutError(_ForeignError):
    pass


def _selection(
    name: str = "native",
    error_type: type[BaseException] = _NativeError,
    timeout_error_type: type[BaseException] = _NativeTimeoutError,
) -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name=name,
        start_driver=AsyncMock(),
        error_type=error_type,
        timeout_error_type=timeout_error_type,
        metadata=BrowserEngineMetadata(name=name),
        selection_reason="test",
    )


def _block() -> CodeBlock:
    return CodeBlock.model_construct(
        label="code",
        code="raise Exception('boom')",
        parameters=[],
        output_parameter=MagicMock(),
    )


def _recording_page(recorded_exception: Exception | None) -> MagicMock:
    page = MagicMock()
    page.last_recorded_exception.return_value = recorded_exception
    return page


def test_recorded_exception_is_always_healable() -> None:
    block = _block()
    exception = PlaywrightError("foreign but recorded")

    assert block._is_healable_page_failure(exception, _recording_page(exception), _selection()) is True


def test_none_selection_preserves_stock_playwright_behavior() -> None:
    block = _block()
    recording_page = _recording_page(None)

    assert block._is_healable_page_failure(PlaywrightError("stock"), recording_page) is True
    assert block._is_healable_page_failure(RuntimeError("user code"), recording_page) is False


def test_selected_engine_native_error_is_healable() -> None:
    assert (
        _block()._is_healable_page_failure(
            _NativeError("native"),
            _recording_page(None),
            _selection(),
        )
        is True
    )


@pytest.mark.parametrize("exception", [PlaywrightError("stock"), _ForeignError("foreign")])
def test_selected_engine_foreign_driver_error_is_not_healable(exception: Exception) -> None:
    assert _block()._is_healable_page_failure(exception, _recording_page(None), _selection()) is False


@pytest.mark.asyncio
async def test_live_legacy_path_uses_run_pinned_engine_as_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    exception = PlaywrightError("stock error from a foreign driver")
    selection = _selection()
    page = MagicMock()
    browser_state = SimpleNamespace(
        engine_selection=selection,
        get_working_page=AsyncMock(return_value=page),
    )
    context = SimpleNamespace(
        organization_id="o_test",
        get_value=MagicMock(),
        mask_secrets_in_data=lambda value: value,
    )

    class _Recorder:
        def __init__(self, **kwargs: object) -> None:
            self.recording_page = _recording_page(None)

        async def create_task_and_step(self) -> None:
            return None

        async def link_block(self) -> None:
            return None

        def recorded_actions(self) -> list[object]:
            return []

        def last_recorded_exception(self) -> None:
            return None

        async def persist(self, actions: list[object]) -> None:
            return None

        async def finalize(self, success: bool) -> None:
            return None

    resolve_failure = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(app.AGENT_FUNCTION, "validate_code_block", AsyncMock(return_value=None))
    monkeypatch.setattr(app.AGENT_FUNCTION, "should_use_codeblock_runner", AsyncMock(return_value=False))
    monkeypatch.setattr(CodeBlock, "get_workflow_run_context", MagicMock(return_value=context))
    monkeypatch.setattr(CodeBlock, "get_or_create_browser_state", AsyncMock(return_value=browser_state))
    monkeypatch.setattr(CodeBlock, "_ensure_run_recording_artifact", AsyncMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "format_potential_template_parameters", MagicMock(return_value=None))
    monkeypatch.setattr(CodeBlock, "generate_async_user_function", MagicMock(return_value=AsyncMock()))
    monkeypatch.setattr(CodeBlock, "execute_user_function_with_timeout", AsyncMock(side_effect=exception))
    monkeypatch.setattr(CodeBlock, "_resolve_failure_with_heal", resolve_failure)
    monkeypatch.setattr(block_module, "CodeBlockActionRecording", _Recorder)

    await _block().execute(
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
    )

    resolve_failure.assert_awaited_once()
    assert resolve_failure.await_args is not None
    classification = resolve_failure.await_args.kwargs["classification"]
    assert classification == HealClassification(healable=False, skip_reason=HealSkipReason.unclassifiable)


@pytest.mark.asyncio
async def test_attempt_self_heal_default_classification_uses_pinned_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = _block()
    exception = PlaywrightError("foreign stock error")
    browser_state = SimpleNamespace(engine_selection=_selection())
    monkeypatch.setattr(CodeBlock, "_self_heal_enabled", AsyncMock(return_value=True))

    result = await block._attempt_self_heal(
        exception=exception,
        failing_line=None,
        recording_page=_recording_page(None),
        workflow_run_context=MagicMock(),
        workflow_run_id="wr_test",
        workflow_run_block_id="wrb_test",
        organization_id="o_test",
        browser_session_id=None,
        browser_state=browser_state,
    )

    assert result is None
