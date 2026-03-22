"""Unit tests for the ATS pipeline core methods: dynamic_field_map, fill_from_mapping, validate_mapping."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.core.script_generations.ats import detect_ats_platform

# ---------------------------------------------------------------------------
# detect_ats_platform
# ---------------------------------------------------------------------------


class TestDetectAtsPlatform:
    def test_lever(self) -> None:
        assert detect_ats_platform("https://jobs.lever.co/company/abc") == "lever"

    def test_lever_domain_only(self) -> None:
        assert detect_ats_platform("jobs.lever.co") == "lever"

    def test_none(self) -> None:
        assert detect_ats_platform(None) is None

    def test_empty(self) -> None:
        assert detect_ats_platform("") is None

    def test_unknown(self) -> None:
        assert detect_ats_platform("example.com") is None


# ---------------------------------------------------------------------------
# dynamic_field_map — index conversion, null filtering, error handling
# ---------------------------------------------------------------------------


def _make_skyvern_page() -> MagicMock:
    """Create a mock SkyvernPage with the methods we need."""
    page = MagicMock()
    page.page = AsyncMock()
    page._ai = MagicMock()
    return page


class TestDynamicFieldMapIndexing:
    """Test the response-parsing logic of dynamic_field_map without actually calling the LLM."""

    @pytest.mark.asyncio
    async def test_1indexed_to_0indexed_conversion(self) -> None:
        """LLM returns 1-indexed keys; result should be 0-indexed."""
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        form_fields = [
            {"label": "Full name", "type": "text"},
            {"label": "Email", "type": "text"},
        ]
        data = {"full_name": "John", "email": "john@test.com"}

        llm_response = {"1": "John", "2": "john@test.com"}

        with (
            patch.object(SkyvernPage, "__init__", lambda self, *a, **kw: None),
            patch("skyvern.core.script_generations.skyvern_page.prompt_engine") as mock_pe,
            patch("skyvern.core.script_generations.skyvern_page.app") as mock_app,
            patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_ctx,
        ):
            mock_pe.load_prompt.return_value = "test prompt"
            mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
            mock_ctx.current.return_value = None

            page = SkyvernPage.__new__(SkyvernPage)
            object.__setattr__(page, "page", MagicMock())
            object.__setattr__(page, "_ai", MagicMock())
            result = await page.dynamic_field_map(form_fields, data)

        assert result == {0: "John", 1: "john@test.com"}

    @pytest.mark.asyncio
    async def test_null_values_filtered(self) -> None:
        """Null values in LLM response should be excluded from the mapping."""
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        form_fields = [
            {"label": "Name", "type": "text"},
            {"label": "Resume", "type": "file"},
        ]
        data = {"name": "John"}

        llm_response = {"1": "John", "2": None}

        with (
            patch.object(SkyvernPage, "__init__", lambda self, *a, **kw: None),
            patch("skyvern.core.script_generations.skyvern_page.prompt_engine") as mock_pe,
            patch("skyvern.core.script_generations.skyvern_page.app") as mock_app,
            patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_ctx,
        ):
            mock_pe.load_prompt.return_value = "test prompt"
            mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
            mock_ctx.current.return_value = None

            page = SkyvernPage.__new__(SkyvernPage)
            object.__setattr__(page, "page", MagicMock())
            object.__setattr__(page, "_ai", MagicMock())
            result = await page.dynamic_field_map(form_fields, data)

        assert result == {0: "John"}
        assert 1 not in result

    @pytest.mark.asyncio
    async def test_out_of_range_indices_ignored(self) -> None:
        """Indices beyond the field list should be silently ignored."""
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        form_fields = [{"label": "Name", "type": "text"}]
        data = {"name": "John"}

        llm_response = {"1": "John", "99": "stray value"}

        with (
            patch.object(SkyvernPage, "__init__", lambda self, *a, **kw: None),
            patch("skyvern.core.script_generations.skyvern_page.prompt_engine") as mock_pe,
            patch("skyvern.core.script_generations.skyvern_page.app") as mock_app,
            patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_ctx,
        ):
            mock_pe.load_prompt.return_value = "test prompt"
            mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value=llm_response)
            mock_ctx.current.return_value = None

            page = SkyvernPage.__new__(SkyvernPage)
            object.__setattr__(page, "page", MagicMock())
            object.__setattr__(page, "_ai", MagicMock())
            result = await page.dynamic_field_map(form_fields, data)

        assert result == {0: "John"}

    @pytest.mark.asyncio
    async def test_non_dict_llm_response_raises(self) -> None:
        """If the LLM returns a list or string instead of a dict, it should raise."""
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        form_fields = [{"label": "Name", "type": "text"}]
        data = {"name": "John"}

        with (
            patch.object(SkyvernPage, "__init__", lambda self, *a, **kw: None),
            patch("skyvern.core.script_generations.skyvern_page.prompt_engine") as mock_pe,
            patch("skyvern.core.script_generations.skyvern_page.app") as mock_app,
            patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_ctx,
        ):
            mock_pe.load_prompt.return_value = "test prompt"
            mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value=["not", "a", "dict"])
            mock_ctx.current.return_value = None

            page = SkyvernPage.__new__(SkyvernPage)
            object.__setattr__(page, "page", MagicMock())
            object.__setattr__(page, "_ai", MagicMock())
            with pytest.raises(ValueError, match="list"):
                await page.dynamic_field_map(form_fields, data)

    @pytest.mark.asyncio
    async def test_empty_inputs_return_empty(self) -> None:
        """Empty form_fields or data should return {} immediately."""
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        with patch.object(SkyvernPage, "__init__", lambda self, *a, **kw: None):
            page = SkyvernPage.__new__(SkyvernPage)
            object.__setattr__(page, "page", MagicMock())
            object.__setattr__(page, "_ai", MagicMock())
            assert await page.dynamic_field_map([], {"name": "John"}) == {}
            assert await page.dynamic_field_map([{"label": "Name"}], {}) == {}


# ---------------------------------------------------------------------------
# validate_mapping
# ---------------------------------------------------------------------------


class TestValidateMapping:
    @pytest.mark.asyncio
    async def test_no_navigation_goal_returns_true(self) -> None:
        """Without a navigation goal, validation should always pass."""
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        with patch.object(SkyvernPage, "__init__", lambda self, *a, **kw: None):
            page = SkyvernPage.__new__(SkyvernPage)
            object.__setattr__(page, "page", MagicMock())
            object.__setattr__(page, "_ai", MagicMock())
            result = await page.validate_mapping([], {}, None)
            assert result is True

            result = await page.validate_mapping([], {}, "")
            assert result is True

    @pytest.mark.asyncio
    async def test_llm_returns_complete(self) -> None:
        """When LLM says complete, validation should return True."""
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        with (
            patch.object(SkyvernPage, "__init__", lambda self, *a, **kw: None),
            patch("skyvern.core.script_generations.skyvern_page.app") as mock_app,
            patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_ctx,
        ):
            mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(return_value={"decision": "complete"})
            mock_ctx.current.return_value = None

            page = SkyvernPage.__new__(SkyvernPage)
            object.__setattr__(page, "page", MagicMock())
            object.__setattr__(page, "_ai", MagicMock())
            result = await page.validate_mapping(
                [{"label": "Name", "type": "text"}],
                {0: "John"},
                "Fill the form",
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_llm_returns_terminate(self) -> None:
        """When LLM says terminate, validation should return False."""
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        with (
            patch.object(SkyvernPage, "__init__", lambda self, *a, **kw: None),
            patch("skyvern.core.script_generations.skyvern_page.app") as mock_app,
            patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_ctx,
        ):
            mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(
                return_value={"decision": "terminate", "reason": "missing data"}
            )
            mock_ctx.current.return_value = None

            page = SkyvernPage.__new__(SkyvernPage)
            object.__setattr__(page, "page", MagicMock())
            object.__setattr__(page, "_ai", MagicMock())
            result = await page.validate_mapping(
                [{"label": "Work auth", "type": "radio_group", "required": True}],
                {},
                "Terminate if work auth is unknown",
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_llm_failure_defaults_to_complete(self) -> None:
        """If the LLM call fails, validation should default to True (complete)."""
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        with (
            patch.object(SkyvernPage, "__init__", lambda self, *a, **kw: None),
            patch("skyvern.core.script_generations.skyvern_page.app") as mock_app,
            patch("skyvern.core.script_generations.skyvern_page.skyvern_context") as mock_ctx,
        ):
            mock_app.SECONDARY_LLM_API_HANDLER = AsyncMock(side_effect=Exception("LLM down"))
            mock_ctx.current.return_value = None

            page = SkyvernPage.__new__(SkyvernPage)
            object.__setattr__(page, "page", MagicMock())
            object.__setattr__(page, "_ai", MagicMock())
            result = await page.validate_mapping(
                [{"label": "Name", "type": "text"}],
                {0: "John"},
                "Fill the form",
            )
            assert result is True


# ---------------------------------------------------------------------------
# fill_from_mapping — option matching logic (no browser needed)
# ---------------------------------------------------------------------------


class TestFillFromMappingOptionMatching:
    """Test the radio/checkbox option matching logic in fill_from_mapping."""

    @pytest.mark.asyncio
    async def test_exact_match_clicks_option(self) -> None:
        """When LLM value exactly matches an option label, it should be clicked."""
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        form_fields = [
            {
                "label": "Are you authorized to work?",
                "type": "radio_group",
                "selector": "input[name='auth']",
                "tag": "input",
                "options": [
                    {"label": "Yes", "value": "Yes", "selector": "#auth-yes"},
                    {"label": "No", "value": "No", "selector": "#auth-no"},
                ],
            }
        ]
        mapping = {0: "Yes"}

        with patch.object(SkyvernPage, "__init__", lambda self, *a, **kw: None):
            page = SkyvernPage.__new__(SkyvernPage)
            object.__setattr__(page, "page", MagicMock())
            object.__setattr__(page, "_ai", MagicMock())
            page.page = AsyncMock()
            page._ai = MagicMock()
            page.click = AsyncMock()
            page._track_ai_call = MagicMock()

            await page.fill_from_mapping(form_fields, mapping)

            # Should click the "Yes" option selector
            page.click.assert_called_once_with(selector="#auth-yes", ai=None)

    @pytest.mark.asyncio
    async def test_substring_match_clicks_option(self) -> None:
        """When LLM value is a substring of an option, it should match."""
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        form_fields = [
            {
                "label": "Veteran status",
                "type": "radio_group",
                "selector": "input[name='vet']",
                "tag": "input",
                "options": [
                    {"label": "I am not a protected veteran", "value": "no", "selector": "#vet-no"},
                    {"label": "I am a protected veteran", "value": "yes", "selector": "#vet-yes"},
                ],
            }
        ]
        mapping = {0: "I am not a protected veteran"}

        with patch.object(SkyvernPage, "__init__", lambda self, *a, **kw: None):
            page = SkyvernPage.__new__(SkyvernPage)
            object.__setattr__(page, "page", MagicMock())
            object.__setattr__(page, "_ai", MagicMock())
            page.page = AsyncMock()
            page._ai = MagicMock()
            page.click = AsyncMock()
            page._track_ai_call = MagicMock()

            await page.fill_from_mapping(form_fields, mapping)

            page.click.assert_called_once_with(selector="#vet-no", ai=None)

    @pytest.mark.asyncio
    async def test_text_field_fills_value(self) -> None:
        """Text fields should be filled with the mapped value."""
        from skyvern.core.script_generations.skyvern_page import SkyvernPage

        form_fields = [{"label": "Full name", "type": "text", "selector": "input[name='name']", "tag": "input"}]
        mapping = {0: "John Smith"}

        with patch.object(SkyvernPage, "__init__", lambda self, *a, **kw: None):
            page = SkyvernPage.__new__(SkyvernPage)
            object.__setattr__(page, "page", MagicMock())
            object.__setattr__(page, "_ai", MagicMock())
            page.page = AsyncMock()
            page._ai = MagicMock()
            page.fill = AsyncMock()
            page._track_ai_call = MagicMock()

            await page.fill_from_mapping(form_fields, mapping)

            page.fill.assert_called_once_with(selector="input[name='name']", value="John Smith", ai=None)
