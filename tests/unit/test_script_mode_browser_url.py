from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.service import _resolve_first_block_url


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


def test_resolve_first_block_url_static() -> None:
    """First block with a static URL resolves to that URL."""
    block = _make_block(url="https://www.example.com/target")
    wfr = _make_workflow_run()
    mock_wrc = MagicMock()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = mock_wrc

        assert _resolve_first_block_url([block], wfr) == "https://www.example.com/target"
        block.format_block_parameter_template_from_workflow_run_context.assert_called_once_with(
            "https://www.example.com/target", mock_wrc
        )


def test_resolve_first_block_url_templated() -> None:
    """A templated URL is resolved against the workflow run context."""
    block = _make_block(url="{{ parameters.target_url }}")
    block.format_block_parameter_template_from_workflow_run_context = MagicMock(
        return_value="https://www.resolved-site.com/page"
    )
    wfr = _make_workflow_run()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = MagicMock()

        assert _resolve_first_block_url([block], wfr) == "https://www.resolved-site.com/page"


def test_resolve_first_block_url_skips_leading_blocks_without_url() -> None:
    """A leading block without a URL (e.g. a code block) is skipped."""
    no_url = _make_block(url=None)
    nav = _make_block(url="https://www.navigates.com")
    wfr = _make_workflow_run()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = MagicMock()

        assert _resolve_first_block_url([no_url, nav], wfr) == "https://www.navigates.com"


def test_resolve_first_block_url_uses_first_of_many() -> None:
    """When several blocks have URLs, the first one wins."""
    first = _make_block(url="https://www.first.com")
    second = _make_block(url="https://www.second.com")
    wfr = _make_workflow_run()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = MagicMock()

        assert _resolve_first_block_url([first, second], wfr) == "https://www.first.com"


def test_resolve_first_block_url_none_when_no_url() -> None:
    """No block has a URL → None."""
    blocks = [_make_block(url=None), _make_block(url="")]
    wfr = _make_workflow_run()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = MagicMock()

        assert _resolve_first_block_url(blocks, wfr) is None


def test_resolve_first_block_url_continues_past_empty_resolution() -> None:
    """A block whose URL resolves to empty falls through to the next block."""
    empties_out = _make_block(url="{{ parameters.missing }}")
    empties_out.format_block_parameter_template_from_workflow_run_context = MagicMock(return_value="")
    nav = _make_block(url="https://www.real.com")
    wfr = _make_workflow_run()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = MagicMock()

        assert _resolve_first_block_url([empties_out, nav], wfr) == "https://www.real.com"


def test_resolve_first_block_url_none_when_context_unavailable() -> None:
    """If the workflow run context can't be fetched, return None instead of raising."""
    block = _make_block(url="https://www.example.com")
    wfr = _make_workflow_run()

    with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.side_effect = RuntimeError("no context")

        assert _resolve_first_block_url([block], wfr) is None


@pytest.mark.asyncio
async def test_script_page_selects_proxy_url_without_navigating() -> None:
    """The script SDK creates the browser with the URL (for proxy selection) but
    navigate=False, so the generated script performs the first goto itself."""
    from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage

    workflow_run = _make_workflow_run(browser_profile_id="bp_1")

    ctx = MagicMock()
    ctx.workflow_run_id = "wfr_test"
    ctx.organization_id = "o_1"

    with (
        patch("skyvern.core.script_generations.script_skyvern_page.app") as mock_app,
        patch("skyvern.core.script_generations.script_skyvern_page.skyvern_context") as mock_context,
    ):
        mock_context.current.return_value = ctx
        mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=workflow_run)
        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run = AsyncMock()

        await ScriptSkyvernPage._get_or_create_browser_state(url="https://www.example.com/target")

        mock_app.BROWSER_MANAGER.get_or_create_for_workflow_run.assert_called_once_with(
            workflow_run=workflow_run,
            url="https://www.example.com/target",
            browser_session_id=None,
            browser_profile_id="bp_1",
            navigate=False,
        )
