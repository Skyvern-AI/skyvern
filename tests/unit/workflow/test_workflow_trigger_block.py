"""Unit tests for WorkflowTriggerBlock template rendering and depth checking."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.exceptions import InvalidWorkflowDefinition
from skyvern.forge.sdk.workflow.models.block import (
    _JSON_TYPE_MARKER,
    FailedToFormatJinjaStyleParameter,
    WorkflowTriggerBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.schemas.workflows import BlockType


def _make_output_parameter() -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        key="__output__",
        output_parameter_id="op_test",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


def _make_block(**overrides: Any) -> WorkflowTriggerBlock:
    """Create a WorkflowTriggerBlock with sensible defaults."""
    defaults: dict[str, Any] = {
        "label": "test_trigger",
        "workflow_permanent_id": "wpid_test",
        "payload": None,
        "wait_for_completion": True,
        "browser_session_id": None,
        "output_parameter": _make_output_parameter(),
    }
    defaults.update(overrides)
    return WorkflowTriggerBlock(**defaults)


class TestRenderTemplateValue:
    """Test _render_template_value: JSON marker stripping and mid-string guard."""

    def _render(self, block: WorkflowTriggerBlock, value: str, rendered_return: str) -> Any:
        ctx = MagicMock()
        with patch.object(
            WorkflowTriggerBlock,
            "format_block_parameter_template_from_workflow_run_context",
            return_value=rendered_return,
        ):
            return block._render_template_value(value, ctx)

    def test_plain_string_passthrough(self) -> None:
        block = _make_block()
        result = self._render(block, "hello", "hello")
        assert result == "hello"

    def test_json_marker_strips_and_parses(self) -> None:
        block = _make_block()
        json_value = f'{_JSON_TYPE_MARKER}{{"key": "val"}}{_JSON_TYPE_MARKER}'
        result = self._render(block, "{{ x | json }}", json_value)
        assert result == {"key": "val"}

    def test_json_marker_with_list(self) -> None:
        block = _make_block()
        json_value = f"{_JSON_TYPE_MARKER}[1, 2, 3]{_JSON_TYPE_MARKER}"
        result = self._render(block, "{{ x | json }}", json_value)
        assert result == [1, 2, 3]

    def test_json_marker_invalid_json_raises(self) -> None:
        block = _make_block()
        json_value = f"{_JSON_TYPE_MARKER}not-valid-json{_JSON_TYPE_MARKER}"
        with pytest.raises(FailedToFormatJinjaStyleParameter):
            self._render(block, "{{ x | json }}", json_value)

    def test_mid_string_json_marker_raises(self) -> None:
        block = _make_block()
        bad_value = f"prefix-{_JSON_TYPE_MARKER}1234{_JSON_TYPE_MARKER}"
        with pytest.raises(FailedToFormatJinjaStyleParameter, match="complete value replacement"):
            self._render(block, "prefix-{{ x | json }}", bad_value)


class TestRenderTemplatesInPayload:
    """Test _render_templates_in_payload: recursion through dicts, lists, and non-strings."""

    def _render_payload(self, block: WorkflowTriggerBlock, payload: dict[str, Any]) -> dict[str, Any]:
        ctx = MagicMock()
        with patch.object(
            WorkflowTriggerBlock,
            "format_block_parameter_template_from_workflow_run_context",
            side_effect=lambda v, _ctx, **kw: v,
        ):
            return block._render_templates_in_payload(payload, ctx)

    def test_flat_string_values(self) -> None:
        block = _make_block()
        result = self._render_payload(block, {"url": "https://example.com", "name": "test"})
        assert result == {"url": "https://example.com", "name": "test"}

    def test_non_string_values_passthrough(self) -> None:
        block = _make_block()
        result = self._render_payload(block, {"count": 42, "active": True, "data": None})
        assert result == {"count": 42, "active": True, "data": None}

    def test_nested_dict(self) -> None:
        block = _make_block()
        result = self._render_payload(block, {"outer": {"inner": "value"}})
        assert result == {"outer": {"inner": "value"}}

    def test_nested_list(self) -> None:
        block = _make_block()
        result = self._render_payload(block, {"items": ["a", "b", "c"]})
        assert result == {"items": ["a", "b", "c"]}

    def test_deeply_nested_structure(self) -> None:
        block = _make_block()
        payload = {
            "level1": {
                "level2": [
                    {"level3": "deep_value"},
                    [1, 2, "three"],
                ],
            },
        }
        result = self._render_payload(block, payload)
        assert result == payload

    def test_mixed_types_in_list(self) -> None:
        block = _make_block()
        result = self._render_payload(block, {"items": ["str", 42, True, None, {"nested": "dict"}]})
        assert result == {"items": ["str", 42, True, None, {"nested": "dict"}]}


class TestCheckTriggerDepth:
    """Test _check_trigger_depth: boundary conditions at/above/below MAX_TRIGGER_DEPTH."""

    @pytest.mark.asyncio
    async def test_no_parent_returns_zero(self) -> None:
        block = _make_block()
        mock_run = MagicMock()
        mock_run.parent_workflow_run_id = None
        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            mock_app.DATABASE.get_workflow_run = AsyncMock(return_value=mock_run)
            depth = await block._check_trigger_depth("wr_current")
        assert depth == 0

    @pytest.mark.asyncio
    async def test_single_parent_returns_one(self) -> None:
        block = _make_block()
        run_with_parent = MagicMock()
        run_with_parent.parent_workflow_run_id = "wr_parent"
        run_no_parent = MagicMock()
        run_no_parent.parent_workflow_run_id = None

        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            mock_app.DATABASE.get_workflow_run = AsyncMock(side_effect=[run_with_parent, run_no_parent])
            depth = await block._check_trigger_depth("wr_current")
        assert depth == 1

    @pytest.mark.asyncio
    async def test_depth_at_max_raises(self) -> None:
        block = _make_block()
        runs = []
        for i in range(block.MAX_TRIGGER_DEPTH + 1):
            run = MagicMock()
            run.parent_workflow_run_id = f"wr_parent_{i}" if i < block.MAX_TRIGGER_DEPTH else None
            runs.append(run)

        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            mock_app.DATABASE.get_workflow_run = AsyncMock(side_effect=runs)
            with pytest.raises(InvalidWorkflowDefinition, match="depth exceeds maximum"):
                await block._check_trigger_depth("wr_current")

    @pytest.mark.asyncio
    async def test_depth_just_below_max_succeeds(self) -> None:
        block = _make_block()
        runs = []
        for i in range(block.MAX_TRIGGER_DEPTH):
            run = MagicMock()
            run.parent_workflow_run_id = f"wr_parent_{i}" if i < block.MAX_TRIGGER_DEPTH - 1 else None
            runs.append(run)

        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            mock_app.DATABASE.get_workflow_run = AsyncMock(side_effect=runs)
            depth = await block._check_trigger_depth("wr_current")
        assert depth == block.MAX_TRIGGER_DEPTH - 1

    @pytest.mark.asyncio
    async def test_run_not_found_returns_zero(self) -> None:
        block = _make_block()
        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            mock_app.DATABASE.get_workflow_run = AsyncMock(return_value=None)
            depth = await block._check_trigger_depth("wr_nonexistent")
        assert depth == 0


class TestBlockMetadata:
    """Verify basic block properties."""

    def test_block_type(self) -> None:
        block = _make_block()
        assert block.block_type == BlockType.WORKFLOW_TRIGGER

    def test_max_trigger_depth_default(self) -> None:
        block = _make_block()
        assert block.MAX_TRIGGER_DEPTH == 10

    def test_get_all_parameters_empty(self) -> None:
        block = _make_block()
        assert block.get_all_parameters("wr_test") == []
