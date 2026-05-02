"""When a loop block has ``next_loop_on_failure=True`` and the final iteration's
body block ends in a failure or terminated state, the loop block as a whole must
report ``BlockStatus.completed``. Otherwise the parent workflow treats the body
failure as the loop's failure and stops, even though the user explicitly asked
the loop to swallow body failures.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.block import (
    Block,
    ForLoopBlock,
    JinjaBranchCriteria,
    LoopBlockExecutedResult,
    NavigationBlock,
    TaskBlock,
    WhileLoopBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.schemas.workflows import BlockResult, BlockStatus


def _make_output_param(label: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"op_{label}",
        key=f"{label}_output",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def _terminated_block_result(output_param: OutputParameter, reason: str = "user-defined error") -> BlockResult:
    return BlockResult(
        success=False,
        output_parameter=output_param,
        output_parameter_value={"failure_reason": reason},
        status=BlockStatus.terminated,
        failure_reason=reason,
    )


def _failed_block_result(output_param: OutputParameter, reason: str = "navigation failed") -> BlockResult:
    return BlockResult(
        success=False,
        output_parameter=output_param,
        output_parameter_value={"failure_reason": reason},
        status=BlockStatus.failed,
        failure_reason=reason,
    )


def _completed_block_result(output_param: OutputParameter) -> BlockResult:
    return BlockResult(
        success=True,
        output_parameter=output_param,
        output_parameter_value={"value": "ok"},
        status=BlockStatus.completed,
    )


@pytest.fixture
def for_loop_with_next_loop_on_failure() -> ForLoopBlock:
    inner = NavigationBlock(
        label="inner_navigation",
        output_parameter=_make_output_param("inner_navigation"),
        url="https://example.com",
        navigation_goal="select Apr 2026",
    )
    return ForLoopBlock(
        label="parent_loop",
        output_parameter=_make_output_param("parent_loop"),
        loop_blocks=[inner],
        next_loop_on_failure=True,
    )


@pytest.fixture
def for_loop_with_inner_next_loop_on_failure() -> ForLoopBlock:
    inner = NavigationBlock(
        label="inner_navigation",
        output_parameter=_make_output_param("inner_navigation"),
        url="https://example.com",
        navigation_goal="select Apr 2026",
        next_loop_on_failure=True,
    )
    return ForLoopBlock(
        label="parent_loop",
        output_parameter=_make_output_param("parent_loop"),
        loop_blocks=[inner],
    )


class TestForLoopParentNextLoopOnFailureSwallowsLastIterationFailure:
    """Parent ForLoopBlock has ``next_loop_on_failure=True``; last iteration's
    body block ends terminated/failed; loop block must report completed so the
    workflow continues past the loop."""

    @pytest.mark.asyncio
    async def test_parent_flag_swallows_terminated_last_iteration(
        self, for_loop_with_next_loop_on_failure: ForLoopBlock
    ) -> None:
        loop_block = for_loop_with_next_loop_on_failure
        inner = loop_block.loop_blocks[0]

        loop_result = LoopBlockExecutedResult(
            outputs_with_loop_values=[
                [{"loop_value": "a"}],
                [{"loop_value": "b"}],
                [{"loop_value": "c"}],
            ],
            block_outputs=[
                _completed_block_result(inner.output_parameter),
                _completed_block_result(inner.output_parameter),
                _terminated_block_result(inner.output_parameter, "Apr 2026 not in dropdown"),
            ],
            last_block=inner,
            natural_completion=True,
        )

        captured: dict[str, Any] = {}

        async def fake_build_block_result(*args: Any, **kwargs: Any) -> BlockResult:
            captured.update(kwargs)
            return _completed_block_result(loop_block.output_parameter)

        with (
            patch.object(Block, "get_workflow_run_context", return_value=MagicMock()),
            patch.object(
                ForLoopBlock,
                "get_loop_over_parameter_values",
                new_callable=AsyncMock,
                return_value=["a", "b", "c"],
            ),
            patch.object(
                ForLoopBlock,
                "execute_loop_helper",
                new_callable=AsyncMock,
                return_value=loop_result,
            ),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch.object(Block, "build_block_result", side_effect=fake_build_block_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
            await loop_block.execute(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                organization_id="org_test",
            )

        assert captured["status"] == BlockStatus.completed
        assert captured["success"] is True

    @pytest.mark.asyncio
    async def test_parent_flag_swallows_failed_last_iteration(
        self, for_loop_with_next_loop_on_failure: ForLoopBlock
    ) -> None:
        loop_block = for_loop_with_next_loop_on_failure
        inner = loop_block.loop_blocks[0]

        loop_result = LoopBlockExecutedResult(
            outputs_with_loop_values=[[{"loop_value": "a"}], [{"loop_value": "b"}]],
            block_outputs=[
                _completed_block_result(inner.output_parameter),
                _failed_block_result(inner.output_parameter, "transient navigation failure"),
            ],
            last_block=inner,
            natural_completion=True,
        )

        captured: dict[str, Any] = {}

        async def fake_build_block_result(*args: Any, **kwargs: Any) -> BlockResult:
            captured.update(kwargs)
            return _completed_block_result(loop_block.output_parameter)

        with (
            patch.object(Block, "get_workflow_run_context", return_value=MagicMock()),
            patch.object(
                ForLoopBlock,
                "get_loop_over_parameter_values",
                new_callable=AsyncMock,
                return_value=["a", "b"],
            ),
            patch.object(
                ForLoopBlock,
                "execute_loop_helper",
                new_callable=AsyncMock,
                return_value=loop_result,
            ),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch.object(Block, "build_block_result", side_effect=fake_build_block_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
            await loop_block.execute(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                organization_id="org_test",
            )

        assert captured["status"] == BlockStatus.completed
        assert captured["success"] is True


class TestForLoopInnerNextLoopOnFailureSwallowsLastIterationFailure:
    """Inner block has ``next_loop_on_failure=True``; last iteration's body
    block ends terminated/failed; loop block must report completed."""

    @pytest.mark.asyncio
    async def test_inner_flag_swallows_terminated_last_iteration(
        self, for_loop_with_inner_next_loop_on_failure: ForLoopBlock
    ) -> None:
        loop_block = for_loop_with_inner_next_loop_on_failure
        inner = loop_block.loop_blocks[0]

        loop_result = LoopBlockExecutedResult(
            outputs_with_loop_values=[[{"loop_value": "a"}], [{"loop_value": "b"}]],
            block_outputs=[
                _completed_block_result(inner.output_parameter),
                _terminated_block_result(inner.output_parameter, "user-defined error fired"),
            ],
            last_block=inner,
            natural_completion=True,
        )

        captured: dict[str, Any] = {}

        async def fake_build_block_result(*args: Any, **kwargs: Any) -> BlockResult:
            captured.update(kwargs)
            return _completed_block_result(loop_block.output_parameter)

        with (
            patch.object(Block, "get_workflow_run_context", return_value=MagicMock()),
            patch.object(
                ForLoopBlock,
                "get_loop_over_parameter_values",
                new_callable=AsyncMock,
                return_value=["a", "b"],
            ),
            patch.object(
                ForLoopBlock,
                "execute_loop_helper",
                new_callable=AsyncMock,
                return_value=loop_result,
            ),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch.object(Block, "build_block_result", side_effect=fake_build_block_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
            await loop_block.execute(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                organization_id="org_test",
            )

        assert captured["status"] == BlockStatus.completed
        assert captured["success"] is True


class TestForLoopWithoutFlagsStillTerminatesOnBodyFailure:
    """Regression guard: without any ``next_loop_on_failure`` flag, the loop
    block must still surface a terminated body failure as terminated."""

    @pytest.mark.asyncio
    async def test_no_flag_propagates_terminated(self) -> None:
        inner = TaskBlock(label="inner", output_parameter=_make_output_param("inner"))
        loop_block = ForLoopBlock(
            label="parent_loop",
            output_parameter=_make_output_param("parent_loop"),
            loop_blocks=[inner],
        )

        loop_result = LoopBlockExecutedResult(
            outputs_with_loop_values=[[{"loop_value": "a"}]],
            block_outputs=[_terminated_block_result(inner.output_parameter, "stop")],
            last_block=inner,
            natural_completion=False,
        )

        captured: dict[str, Any] = {}

        async def fake_build_block_result(*args: Any, **kwargs: Any) -> BlockResult:
            captured.update(kwargs)
            return _terminated_block_result(loop_block.output_parameter)

        with (
            patch.object(Block, "get_workflow_run_context", return_value=MagicMock()),
            patch.object(
                ForLoopBlock,
                "get_loop_over_parameter_values",
                new_callable=AsyncMock,
                return_value=["a"],
            ),
            patch.object(
                ForLoopBlock,
                "execute_loop_helper",
                new_callable=AsyncMock,
                return_value=loop_result,
            ),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch.object(Block, "build_block_result", side_effect=fake_build_block_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
            await loop_block.execute(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                organization_id="org_test",
            )

        assert captured["status"] == BlockStatus.terminated
        assert captured["success"] is False


class TestWhileLoopParentNextLoopOnFailureSwallowsLastIterationFailure:
    @pytest.mark.asyncio
    async def test_parent_flag_swallows_terminated_last_iteration(self) -> None:
        inner = NavigationBlock(
            label="inner_navigation",
            output_parameter=_make_output_param("inner_navigation"),
            url="https://example.com",
            navigation_goal="iterate",
        )
        loop_block = WhileLoopBlock(
            label="parent_while",
            output_parameter=_make_output_param("parent_while"),
            loop_blocks=[inner],
            condition=JinjaBranchCriteria(expression="{{ keep_going }}"),
            next_loop_on_failure=True,
        )

        loop_result = LoopBlockExecutedResult(
            outputs_with_loop_values=[[{"v": 1}], [{"v": 2}]],
            block_outputs=[
                _completed_block_result(inner.output_parameter),
                _terminated_block_result(inner.output_parameter, "user-defined error"),
            ],
            last_block=inner,
            natural_completion=True,
        )

        captured: dict[str, Any] = {}

        async def fake_build_block_result(*args: Any, **kwargs: Any) -> BlockResult:
            captured.update(kwargs)
            return _completed_block_result(loop_block.output_parameter)

        with (
            patch.object(Block, "get_workflow_run_context", return_value=MagicMock()),
            patch.object(
                WhileLoopBlock,
                "_execute_while_loop_helper",
                new_callable=AsyncMock,
                return_value=loop_result,
            ),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch.object(Block, "build_block_result", side_effect=fake_build_block_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
            await loop_block.execute(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                organization_id="org_test",
            )

        assert captured["status"] == BlockStatus.completed
        assert captured["success"] is True


class TestLoopBlockSwallowPathClearsFailureReason:
    """Codex review: when ``parent_next_loop_swallow`` flips status to
    completed but ``is_completed()`` still returns False, the loop must drop
    the inner block's ``failure_reason`` so workflow summaries don't pick up
    a failure on a successful run."""

    @pytest.mark.asyncio
    async def test_for_loop_parent_flag_swallow_clears_failure_reason(
        self, for_loop_with_next_loop_on_failure: ForLoopBlock
    ) -> None:
        loop_block = for_loop_with_next_loop_on_failure
        inner = loop_block.loop_blocks[0]

        loop_result = LoopBlockExecutedResult(
            outputs_with_loop_values=[[{"loop_value": "a"}], [{"loop_value": "b"}]],
            block_outputs=[
                _completed_block_result(inner.output_parameter),
                _terminated_block_result(inner.output_parameter, "Apr 2026 not in dropdown"),
            ],
            last_block=inner,
            natural_completion=True,
        )

        captured: dict[str, Any] = {}

        async def fake_build_block_result(*args: Any, **kwargs: Any) -> BlockResult:
            captured.update(kwargs)
            return _completed_block_result(loop_block.output_parameter)

        with (
            patch.object(Block, "get_workflow_run_context", return_value=MagicMock()),
            patch.object(
                ForLoopBlock,
                "get_loop_over_parameter_values",
                new_callable=AsyncMock,
                return_value=["a", "b"],
            ),
            patch.object(
                ForLoopBlock,
                "execute_loop_helper",
                new_callable=AsyncMock,
                return_value=loop_result,
            ),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch.object(Block, "build_block_result", side_effect=fake_build_block_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
            await loop_block.execute(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                organization_id="org_test",
            )

        assert captured["status"] == BlockStatus.completed
        assert captured["success"] is True
        assert captured["failure_reason"] is None

    @pytest.mark.asyncio
    async def test_while_loop_parent_flag_swallow_clears_failure_reason(self) -> None:
        inner = NavigationBlock(
            label="inner_navigation",
            output_parameter=_make_output_param("inner_navigation"),
            url="https://example.com",
            navigation_goal="iterate",
        )
        loop_block = WhileLoopBlock(
            label="parent_while",
            output_parameter=_make_output_param("parent_while"),
            loop_blocks=[inner],
            condition=JinjaBranchCriteria(expression="{{ keep_going }}"),
            next_loop_on_failure=True,
        )

        loop_result = LoopBlockExecutedResult(
            outputs_with_loop_values=[[{"v": 1}]],
            block_outputs=[_terminated_block_result(inner.output_parameter, "user-defined error")],
            last_block=inner,
            natural_completion=True,
        )

        captured: dict[str, Any] = {}

        async def fake_build_block_result(*args: Any, **kwargs: Any) -> BlockResult:
            captured.update(kwargs)
            return _completed_block_result(loop_block.output_parameter)

        with (
            patch.object(Block, "get_workflow_run_context", return_value=MagicMock()),
            patch.object(
                WhileLoopBlock,
                "_execute_while_loop_helper",
                new_callable=AsyncMock,
                return_value=loop_result,
            ),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch.object(Block, "build_block_result", side_effect=fake_build_block_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
            await loop_block.execute(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                organization_id="org_test",
            )

        assert captured["status"] == BlockStatus.completed
        assert captured["success"] is True
        assert captured["failure_reason"] is None


class TestSyntheticSafetyLimitNotSwallowedByNextLoopOnFailure:
    """Pre-existing safety-limit semantics: hitting ``max_steps_per_iteration`` on
    the last iteration must still fail the loop even when ``next_loop_on_failure``
    is set; the flag governs body failures, not safety caps."""

    @pytest.mark.asyncio
    async def test_for_loop_max_steps_per_iter_on_last_iteration_still_fails(
        self, for_loop_with_next_loop_on_failure: ForLoopBlock
    ) -> None:
        loop_block = for_loop_with_next_loop_on_failure
        inner = loop_block.loop_blocks[0]

        synthetic_max_steps_failure = BlockResult(
            success=False,
            output_parameter=loop_block.output_parameter,
            output_parameter_value=None,
            status=BlockStatus.failed,
            failure_reason="Reached max_steps_per_iteration limit of 30",
            is_synthetic_loop_failure=True,
        )
        loop_result = LoopBlockExecutedResult(
            outputs_with_loop_values=[[{"loop_value": "a"}], [{"loop_value": "b"}]],
            block_outputs=[
                _completed_block_result(inner.output_parameter),
                synthetic_max_steps_failure,
            ],
            last_block=inner,
            natural_completion=True,
        )

        captured: dict[str, Any] = {}

        async def fake_build_block_result(*args: Any, **kwargs: Any) -> BlockResult:
            captured.update(kwargs)
            return BlockResult(
                success=False,
                output_parameter=loop_block.output_parameter,
                status=BlockStatus.failed,
            )

        with (
            patch.object(Block, "get_workflow_run_context", return_value=MagicMock()),
            patch.object(
                ForLoopBlock,
                "get_loop_over_parameter_values",
                new_callable=AsyncMock,
                return_value=["a", "b"],
            ),
            patch.object(
                ForLoopBlock,
                "execute_loop_helper",
                new_callable=AsyncMock,
                return_value=loop_result,
            ),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch.object(Block, "build_block_result", side_effect=fake_build_block_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
            await loop_block.execute(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                organization_id="org_test",
            )

        assert captured["status"] == BlockStatus.failed
        assert captured["success"] is False
        assert captured["failure_reason"] == "Reached max_steps_per_iteration limit of 30"

    def test_is_synthetic_loop_failure_detects_loop_owned_output_parameter(self) -> None:
        loop_op = _make_output_param("parent_loop")
        inner = NavigationBlock(
            label="inner",
            output_parameter=_make_output_param("inner"),
            url="https://example.com",
            navigation_goal="g",
        )
        synthetic = BlockResult(
            success=False,
            output_parameter=loop_op,
            output_parameter_value=None,
            status=BlockStatus.failed,
            failure_reason="Reached max_steps_per_iteration limit of 30",
            is_synthetic_loop_failure=True,
        )
        result = LoopBlockExecutedResult(
            outputs_with_loop_values=[],
            block_outputs=[synthetic],
            last_block=inner,
            natural_completion=True,
        )
        assert result.is_synthetic_loop_failure() is True
        assert result.is_completed() is False

    def test_is_synthetic_loop_failure_returns_false_for_real_child_failure(self) -> None:
        inner = NavigationBlock(
            label="inner",
            output_parameter=_make_output_param("inner"),
            url="https://example.com",
            navigation_goal="g",
            next_loop_on_failure=True,
        )
        result = LoopBlockExecutedResult(
            outputs_with_loop_values=[],
            block_outputs=[_terminated_block_result(inner.output_parameter)],
            last_block=inner,
            natural_completion=True,
        )
        assert result.is_synthetic_loop_failure() is False
        assert result.is_completed() is True


class TestLoopBlockExecutedResultIsCompletedRespectsNaturalCompletion:
    """Without natural_completion the swallow flags must not mark a structurally
    failed loop completed. block_outputs[-1] is a synthetic loop-level failure
    on early-return paths (max iterations, missing block label), yet last_block
    still points at a previously-executed child whose flag would otherwise leak
    through and mask the loop-level error."""

    def test_continue_on_failure_does_not_mask_structural_failure(self) -> None:
        previous_child = NavigationBlock(
            label="prev",
            output_parameter=_make_output_param("prev"),
            url="https://example.com",
            navigation_goal="g",
            continue_on_failure=True,
        )
        synthetic_max_iter_failure = BlockResult(
            success=False,
            output_parameter=_make_output_param("loop"),
            output_parameter_value=None,
            status=BlockStatus.failed,
            failure_reason="Reached max_loop_iterations limit of 100",
            is_synthetic_loop_failure=True,
        )
        result = LoopBlockExecutedResult(
            outputs_with_loop_values=[],
            block_outputs=[synthetic_max_iter_failure],
            last_block=previous_child,
            natural_completion=False,
        )
        assert result.is_completed() is False

    def test_next_loop_on_failure_does_not_mask_structural_failure(self) -> None:
        previous_child = NavigationBlock(
            label="prev",
            output_parameter=_make_output_param("prev"),
            url="https://example.com",
            navigation_goal="g",
            next_loop_on_failure=True,
        )
        synthetic_failure = BlockResult(
            success=False,
            output_parameter=_make_output_param("loop"),
            output_parameter_value=None,
            status=BlockStatus.failed,
            failure_reason="Unable to find block with label foo inside loop bar",
            is_synthetic_loop_failure=True,
        )
        result = LoopBlockExecutedResult(
            outputs_with_loop_values=[],
            block_outputs=[synthetic_failure],
            last_block=previous_child,
            natural_completion=False,
        )
        assert result.is_completed() is False


class TestLoopBlockExecutedResultIsCompletedWithNaturalCompletion:
    def test_is_completed_true_when_natural_completion_and_inner_next_loop_on_failure(self) -> None:
        inner = NavigationBlock(
            label="inner",
            output_parameter=_make_output_param("inner"),
            url="https://example.com",
            navigation_goal="g",
            next_loop_on_failure=True,
        )
        result = LoopBlockExecutedResult(
            outputs_with_loop_values=[],
            block_outputs=[_terminated_block_result(inner.output_parameter)],
            last_block=inner,
            natural_completion=True,
        )
        assert result.is_completed() is True

    def test_is_completed_false_when_natural_completion_but_no_flag(self) -> None:
        inner = NavigationBlock(
            label="inner",
            output_parameter=_make_output_param("inner"),
            url="https://example.com",
            navigation_goal="g",
        )
        result = LoopBlockExecutedResult(
            outputs_with_loop_values=[],
            block_outputs=[_terminated_block_result(inner.output_parameter)],
            last_block=inner,
            natural_completion=True,
        )
        assert result.is_completed() is False

    def test_is_completed_false_when_no_natural_completion_even_with_flag(self) -> None:
        inner = NavigationBlock(
            label="inner",
            output_parameter=_make_output_param("inner"),
            url="https://example.com",
            navigation_goal="g",
            next_loop_on_failure=True,
        )
        result = LoopBlockExecutedResult(
            outputs_with_loop_values=[],
            block_outputs=[_terminated_block_result(inner.output_parameter)],
            last_block=inner,
            natural_completion=False,
        )
        assert result.is_completed() is False
