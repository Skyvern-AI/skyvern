from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.service import _precreate_script_browser


def _make_block(url: str | None = None) -> MagicMock:
    block = MagicMock()
    block.url = url
    block.label = "TestBlock"
    block.format_block_parameter_template_from_workflow_run_context = MagicMock(
        side_effect=lambda val, ctx: val  # identity by default
    )
    return block


def _make_workflow_run(
    workflow_run_id: str = "wfr_test",
    browser_profile_id: str | None = None,
) -> MagicMock:
    wfr = MagicMock()
    wfr.workflow_run_id = workflow_run_id
    wfr.browser_profile_id = browser_profile_id
    return wfr


@pytest.mark.asyncio
async def test_precreate_with_static_url() -> None:
    """Block with a static URL: browser pre-created with that URL."""
    block = _make_block(url="https://www.example.com/target")
    wfr = _make_workflow_run()
    mock_wrc = MagicMock()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = mock_wrc
        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run = AsyncMock()

        await _precreate_script_browser(
            block=block,
            workflow_run=wfr,
            browser_session_id="bs_123",
        )

        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run.assert_called_once_with(
            workflow_run=wfr,
            url="https://www.example.com/target",
            browser_session_id="bs_123",
            browser_profile_id=wfr.browser_profile_id,
        )
        block.format_block_parameter_template_from_workflow_run_context.assert_called_once_with(
            "https://www.example.com/target", mock_wrc
        )


@pytest.mark.asyncio
async def test_precreate_with_templated_url() -> None:
    """Block with a templated URL: resolved URL is passed to browser creation."""
    block = _make_block(url="{{ parameters.target_url }}")
    block.format_block_parameter_template_from_workflow_run_context = MagicMock(
        return_value="https://www.resolved-site.com/page"
    )
    wfr = _make_workflow_run()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = MagicMock()
        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run = AsyncMock()

        await _precreate_script_browser(
            block=block,
            workflow_run=wfr,
            browser_session_id=None,
        )

        call_kwargs = mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run.call_args
        assert call_kwargs.kwargs["url"] == "https://www.resolved-site.com/page"


@pytest.mark.asyncio
async def test_no_precreation_when_url_missing() -> None:
    """Block without URL: no browser pre-creation, no app calls."""
    block = _make_block(url=None)
    wfr = _make_workflow_run()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        await _precreate_script_browser(
            block=block,
            workflow_run=wfr,
            browser_session_id=None,
        )

        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.assert_not_called()
        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run.assert_not_called()


@pytest.mark.asyncio
async def test_no_precreation_when_url_empty_string() -> None:
    """Block with empty-string URL: treated as falsy, no pre-creation."""
    block = _make_block(url="")
    wfr = _make_workflow_run()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        await _precreate_script_browser(
            block=block,
            workflow_run=wfr,
            browser_session_id=None,
        )

        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run.assert_not_called()


@pytest.mark.asyncio
async def test_no_precreation_when_block_lacks_url_attr() -> None:
    """Block type without a url attribute: skipped via getattr guard."""
    block = MagicMock(spec=[])  # empty spec = no attributes
    wfr = _make_workflow_run()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        await _precreate_script_browser(
            block=block,
            workflow_run=wfr,
            browser_session_id=None,
        )

        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run.assert_not_called()


@pytest.mark.asyncio
async def test_browser_creation_failure_propagates() -> None:
    """When browser creation raises, the exception propagates to the caller."""
    block = _make_block(url="https://www.example.com")
    wfr = _make_workflow_run()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = MagicMock()
        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run = AsyncMock(
            side_effect=RuntimeError("browser creation failed")
        )

        with pytest.raises(RuntimeError, match="browser creation failed"):
            await _precreate_script_browser(
                block=block,
                workflow_run=wfr,
                browser_session_id=None,
            )
