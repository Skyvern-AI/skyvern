"""Tests for multi-field TOTP support in script generation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.core.script_generations.generate_script import _annotate_multi_field_totp_sequence
from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.handler import _handle_multi_field_totp_sequence


class _FakeTotp:
    interval = 45

    def __init__(self) -> None:
        self.at_values: list[int] = []

    def at(self, value: int) -> str:
        self.at_values.append(value)
        return f"code-at-{value}"

    def now(self) -> str:
        return "current-code"


class TestAnnotateMultiFieldTotpSequence:
    """Tests for _annotate_multi_field_totp_sequence function."""

    def test_empty_actions(self) -> None:
        """Empty action list returns unchanged."""
        result = _annotate_multi_field_totp_sequence([])
        assert result == []

    def test_less_than_4_actions_returns_unchanged(self) -> None:
        """Actions with fewer than 4 items return unchanged (minimum for TOTP)."""
        actions = [
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp", "text": "1"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp", "text": "2"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp", "text": "3"},
        ]
        result = _annotate_multi_field_totp_sequence(actions)
        # No totp_timing_info should be added
        for action in result:
            assert "totp_timing_info" not in action


class TestHandleMultiFieldTotpSequence:
    @pytest.mark.asyncio
    async def test_next_window_cache_uses_parsed_interval_for_later_digit_wait(self) -> None:
        context = SimpleNamespace(totp_codes={})
        fake_totp = _FakeTotp()
        task = SimpleNamespace(task_id="task_1")

        with (
            patch("skyvern.webeye.actions.handler.skyvern_context.ensure_context", return_value=context),
            patch("skyvern.webeye.actions.handler.parse_totp_config", return_value=fake_totp),
            patch("skyvern.webeye.actions.handler.time.time", return_value=44),
        ):
            result = await _handle_multi_field_totp_sequence(
                {"action_index": 0, "totp_secret": "otpauth://totp/example?secret=abc"},
                task,
            )

        assert result is None
        assert fake_totp.at_values == [45]
        assert context.totp_codes["task_1_totp_cache"] == "code-at-45"
        assert context.totp_codes["task_1_totp_cache_valid_from"] == "45"
        assert context.totp_codes["task_1_totp_cache_valid_until"] == "90"

        with (
            patch("skyvern.webeye.actions.handler.skyvern_context.ensure_context", return_value=context),
            patch("skyvern.webeye.actions.handler.parse_totp_config", return_value=fake_totp),
            patch("skyvern.webeye.actions.handler.time.time", return_value=44),
            patch("skyvern.webeye.actions.handler.asyncio.sleep", new_callable=AsyncMock) as sleep_mock,
        ):
            result = await _handle_multi_field_totp_sequence(
                {"action_index": 5, "totp_secret": "otpauth://totp/example?secret=abc"},
                task,
            )

        assert result is None
        sleep_mock.assert_awaited_once_with(1)

    def test_4_digit_sequence_gets_annotated(self) -> None:
        """4 consecutive single-digit inputs with same field_name get annotated."""
        actions = [
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp_code", "text": "1"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp_code", "text": "2"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp_code", "text": "3"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp_code", "text": "4"},
        ]
        result = _annotate_multi_field_totp_sequence(actions)

        for idx, action in enumerate(result):
            assert "totp_timing_info" in action
            assert action["totp_timing_info"]["is_totp_sequence"] is True
            assert action["totp_timing_info"]["action_index"] == idx
            assert action["totp_timing_info"]["total_digits"] == 4
            assert action["totp_timing_info"]["field_name"] == "totp_code"

    def test_6_digit_sequence_gets_annotated(self) -> None:
        """Standard 6-digit TOTP sequence gets properly annotated."""
        actions = [
            {"action_type": ActionType.INPUT_TEXT, "field_name": "otp", "text": "1"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "otp", "text": "2"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "otp", "text": "3"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "otp", "text": "4"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "otp", "text": "5"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "otp", "text": "6"},
        ]
        result = _annotate_multi_field_totp_sequence(actions)

        for idx, action in enumerate(result):
            assert action["totp_timing_info"]["action_index"] == idx
            assert action["totp_timing_info"]["total_digits"] == 6

    def test_8_digit_sequence_gets_annotated(self) -> None:
        """8-digit sequence (some TOTP implementations) gets annotated."""
        actions = [{"action_type": ActionType.INPUT_TEXT, "field_name": "code", "text": str(i)} for i in range(8)]
        result = _annotate_multi_field_totp_sequence(actions)

        assert all("totp_timing_info" in a for a in result)
        assert result[0]["totp_timing_info"]["total_digits"] == 8
        assert result[7]["totp_timing_info"]["action_index"] == 7

    def test_3_digits_not_annotated(self) -> None:
        """3 consecutive digits should NOT be annotated (minimum is 4)."""
        actions = [
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code", "text": "1"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code", "text": "2"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code", "text": "3"},
            {"action_type": ActionType.CLICK, "element_id": "submit"},
        ]
        result = _annotate_multi_field_totp_sequence(actions)

        for action in result:
            assert "totp_timing_info" not in action

    def test_different_field_names_not_grouped(self) -> None:
        """Actions with different field_names should not be grouped together."""
        actions = [
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp1", "text": "1"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp1", "text": "2"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp2", "text": "3"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp2", "text": "4"},
        ]
        result = _annotate_multi_field_totp_sequence(actions)

        # Neither sequence has 4+ with same field_name
        for action in result:
            assert "totp_timing_info" not in action

    def test_mixed_actions_with_totp_sequence(self) -> None:
        """TOTP sequence surrounded by non-TOTP actions still gets annotated."""
        actions = [
            {"action_type": ActionType.CLICK, "element_id": "show_totp"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp", "text": "1"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp", "text": "2"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp", "text": "3"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp", "text": "4"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp", "text": "5"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "totp", "text": "6"},
            {"action_type": ActionType.CLICK, "element_id": "submit"},
        ]
        result = _annotate_multi_field_totp_sequence(actions)

        # First and last actions should not have totp_timing_info
        assert "totp_timing_info" not in result[0]
        assert "totp_timing_info" not in result[7]

        # Middle 6 actions should be annotated
        for idx in range(1, 7):
            assert "totp_timing_info" in result[idx]
            assert result[idx]["totp_timing_info"]["action_index"] == idx - 1
            assert result[idx]["totp_timing_info"]["total_digits"] == 6

    def test_multiple_sequences_in_action_list(self) -> None:
        """Multiple separate TOTP sequences in same action list get annotated separately."""
        actions = [
            # First sequence - 4 digits
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code1", "text": "1"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code1", "text": "2"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code1", "text": "3"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code1", "text": "4"},
            # Non-TOTP action breaks the sequence
            {"action_type": ActionType.CLICK, "element_id": "next"},
            # Second sequence - 6 digits
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code2", "text": "5"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code2", "text": "6"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code2", "text": "7"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code2", "text": "8"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code2", "text": "9"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code2", "text": "0"},
        ]
        result = _annotate_multi_field_totp_sequence(actions)

        # First sequence (indices 0-3)
        for idx in range(4):
            assert result[idx]["totp_timing_info"]["total_digits"] == 4
            assert result[idx]["totp_timing_info"]["field_name"] == "code1"

        # Click action (index 4)
        assert "totp_timing_info" not in result[4]

        # Second sequence (indices 5-10)
        for idx in range(5, 11):
            assert result[idx]["totp_timing_info"]["total_digits"] == 6
            assert result[idx]["totp_timing_info"]["field_name"] == "code2"
            assert result[idx]["totp_timing_info"]["action_index"] == idx - 5

    def test_non_digit_text_not_annotated(self) -> None:
        """Actions with non-digit text should not be considered TOTP."""
        actions = [
            {"action_type": ActionType.INPUT_TEXT, "field_name": "field", "text": "a"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "field", "text": "b"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "field", "text": "c"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "field", "text": "d"},
        ]
        result = _annotate_multi_field_totp_sequence(actions)

        for action in result:
            assert "totp_timing_info" not in action

    def test_multi_digit_text_not_annotated(self) -> None:
        """Actions with multi-digit text should not be considered multi-field TOTP."""
        actions = [
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code", "text": "12"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code", "text": "34"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code", "text": "56"},
            {"action_type": ActionType.INPUT_TEXT, "field_name": "code", "text": "78"},
        ]
        result = _annotate_multi_field_totp_sequence(actions)

        for action in result:
            assert "totp_timing_info" not in action

    def test_missing_field_name_not_annotated(self) -> None:
        """Actions without field_name should not be considered TOTP."""
        actions = [
            {"action_type": ActionType.INPUT_TEXT, "text": "1"},
            {"action_type": ActionType.INPUT_TEXT, "text": "2"},
            {"action_type": ActionType.INPUT_TEXT, "text": "3"},
            {"action_type": ActionType.INPUT_TEXT, "text": "4"},
        ]
        result = _annotate_multi_field_totp_sequence(actions)

        for action in result:
            assert "totp_timing_info" not in action


class TestGetTotpDigitBasic:
    """Basic tests for get_totp_digit in ScriptSkyvernPage."""

    @pytest.fixture
    def mock_skyvern_context(self) -> MagicMock:
        """Create a mock skyvern context."""
        ctx = MagicMock()
        ctx.workflow_run_id = "wfr_test123"
        return ctx

    @pytest.mark.asyncio
    async def test_returns_single_digit(
        self,
        mock_skyvern_context: MagicMock,
    ) -> None:
        """get_totp_digit should return a single digit string."""
        # Empty credentials - will fall back to get_actual_value
        mock_workflow_context = MagicMock()
        mock_workflow_context.values = {}

        with patch("skyvern.core.script_generations.script_skyvern_page.skyvern_context") as mock_ctx_module:
            with patch("skyvern.core.script_generations.script_skyvern_page.app") as mock_app:
                mock_ctx_module.ensure_context.return_value = mock_skyvern_context
                mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context = AsyncMock(
                    return_value=mock_workflow_context
                )

                page = MagicMock(spec=ScriptSkyvernPage)
                page._totp_sequence_cache = {}
                page.get_actual_value = AsyncMock(return_value="123456")

                result = await ScriptSkyvernPage.get_totp_digit(
                    page,
                    context=MagicMock(),
                    field_name="totp_code",
                    digit_index=0,
                )

                # Should return a single digit
                assert len(result) == 1
                assert result.isdigit()
                assert result == "1"  # First digit of "123456"

    @pytest.mark.asyncio
    async def test_returns_correct_digit_index(
        self,
        mock_skyvern_context: MagicMock,
    ) -> None:
        """get_totp_digit should return the correct digit for the given index."""
        mock_workflow_context = MagicMock()
        mock_workflow_context.values = {}

        with patch("skyvern.core.script_generations.script_skyvern_page.skyvern_context") as mock_ctx_module:
            with patch("skyvern.core.script_generations.script_skyvern_page.app") as mock_app:
                mock_ctx_module.ensure_context.return_value = mock_skyvern_context
                mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context = AsyncMock(
                    return_value=mock_workflow_context
                )

                page = MagicMock(spec=ScriptSkyvernPage)
                page._totp_sequence_cache = {}
                page.get_actual_value = AsyncMock(return_value="987654")

                # Test each digit index
                for idx, expected in enumerate("987654"):
                    result = await ScriptSkyvernPage.get_totp_digit(
                        page,
                        context=MagicMock(),
                        field_name="totp_code",
                        digit_index=idx,
                    )
                    assert result == expected, f"Expected digit {expected} at index {idx}, got {result}"
