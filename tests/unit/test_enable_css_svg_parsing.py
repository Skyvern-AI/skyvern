"""Tests for the ENABLE_CSS_SVG_PARSING setting gating SVG/CSS conversion."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.agent_functions import AgentFunction, _should_css_shape_convert


def test_should_css_shape_convert_eligible_element() -> None:
    """CSS conversion should be considered for eligible elements."""
    element = {"id": "icon-1", "tagName": "i", "attributes": {}}
    assert _should_css_shape_convert(element) is True


def test_should_css_shape_convert_ineligible_tag() -> None:
    """CSS conversion should be skipped for non-eligible tags."""
    element = {"id": "div-1", "tagName": "div", "attributes": {}}
    assert _should_css_shape_convert(element) is False


def test_should_css_shape_convert_no_id() -> None:
    """CSS conversion should be skipped for elements without an id."""
    element = {"tagName": "i", "attributes": {}}
    assert _should_css_shape_convert(element) is False


@pytest.mark.asyncio
async def test_cleanup_element_tree_disables_conversion_when_setting_off() -> None:
    """When ENABLE_CSS_SVG_PARSING is False, SVG/CSS conversion should be disabled."""
    agent_fn = AgentFunction()

    mock_frame = MagicMock()
    mock_frame.url = "https://example.com"

    element_tree = [
        {
            "id": "svg-1",
            "tagName": "svg",
            "attributes": {"innerHTML": "<svg></svg>"},
        },
        {
            "id": "icon-1",
            "tagName": "i",
            "attributes": {},
        },
    ]

    with (
        patch("skyvern.forge.agent_functions.settings") as mock_settings,
        patch("skyvern.forge.agent_functions.app") as mock_app,
        patch("skyvern.forge.agent_functions.skyvern_context") as mock_ctx,
        patch("skyvern.forge.agent_functions.SkyvernFrame") as mock_sf,
        patch("skyvern.forge.agent_functions._check_svg_eligibility", new_callable=AsyncMock) as mock_svg_check,
        patch("skyvern.forge.agent_functions._convert_css_shape_to_string", new_callable=AsyncMock) as mock_css_convert,
    ):
        mock_settings.SVG_MAX_PARSING_ELEMENT_CNT = 3000
        mock_settings.ENABLE_CSS_SVG_PARSING = False

        mock_app.SVG_CSS_CONVERTER_LLM_API_HANDLER = MagicMock()

        mock_context = MagicMock()
        mock_context.frame_index_map = {}
        mock_ctx.ensure_context.return_value = mock_context

        mock_sf.create_instance = AsyncMock(return_value=MagicMock())

        mock_svg_check.return_value = False

        cleanup_fn = agent_fn.cleanup_element_tree_factory(task=None, step=None)
        await cleanup_fn(mock_frame, "https://example.com", element_tree)

        # SVG check should be called with always_drop=True since conversion is disabled
        for call in mock_svg_check.call_args_list:
            assert call.kwargs.get("always_drop", call.args[4] if len(call.args) > 4 else None) is True

        # CSS conversion should never be called since conversion is disabled
        mock_css_convert.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_element_tree_enables_conversion_when_setting_on() -> None:
    """When ENABLE_CSS_SVG_PARSING is True, SVG/CSS conversion should proceed normally."""
    agent_fn = AgentFunction()

    mock_frame = MagicMock()
    mock_frame.url = "https://example.com"

    element_tree = [
        {
            "id": "icon-1",
            "tagName": "i",
            "attributes": {},
        },
    ]

    with (
        patch("skyvern.forge.agent_functions.settings") as mock_settings,
        patch("skyvern.forge.agent_functions.app") as mock_app,
        patch("skyvern.forge.agent_functions.skyvern_context") as mock_ctx,
        patch("skyvern.forge.agent_functions.SkyvernFrame") as mock_sf,
        patch("skyvern.forge.agent_functions._check_svg_eligibility", new_callable=AsyncMock) as mock_svg_check,
        patch("skyvern.forge.agent_functions._convert_css_shape_to_string", new_callable=AsyncMock) as mock_css_convert,
    ):
        mock_settings.SVG_MAX_PARSING_ELEMENT_CNT = 3000
        mock_settings.ENABLE_CSS_SVG_PARSING = True

        mock_app.SVG_CSS_CONVERTER_LLM_API_HANDLER = MagicMock()

        mock_context = MagicMock()
        mock_context.frame_index_map = {}
        mock_ctx.ensure_context.return_value = mock_context

        mock_sf.create_instance = AsyncMock(return_value=MagicMock())

        mock_svg_check.return_value = False

        cleanup_fn = agent_fn.cleanup_element_tree_factory(task=None, step=None)
        await cleanup_fn(mock_frame, "https://example.com", element_tree)

        # CSS conversion should be called since the element is eligible and conversion is enabled
        mock_css_convert.assert_called_once()
