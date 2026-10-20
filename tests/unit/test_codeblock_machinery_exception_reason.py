from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.workflow.models.block import Block, CodeBlock, ForLoopBlock
from skyvern.forge.sdk.workflow.models.code_block_recorder import CODE_BLOCK_FILENAME, CODE_LINE_OFFSET
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from tests.unit.test_block_description_caching import _setup_mocks

SENTINEL = "secret-param-value"
USER_SOURCE_MARKER = "user_source_marker_line"
USER_CODE_LINE = 3
LEAKY_NAME = "LeakyUnboundError"


class SecretError(Exception):
    __module__ = "skyvern.code_block"


def _output_parameter() -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(output_parameter_id="op_1", key="out", workflow_id="wf_1", created_at=now, modified_at=now)


async def _raise_from_user_code_frame(*args: object, **kwargs: object) -> None:
    source = "\n".join(
        [f"{USER_SOURCE_MARKER} = 1", *[""] * (CODE_LINE_OFFSET + USER_CODE_LINE - 2), f"raise KeyError({SENTINEL!r})"]
    )
    exec(compile(source, CODE_BLOCK_FILENAME, "exec"), {})


async def _raise_user_defined(*args: object, **kwargs: object) -> None:
    raise SecretError(SENTINEL)


async def _raise_index_error(*args: object, **kwargs: object) -> None:
    raise IndexError(SENTINEL)


async def _raise_unbound_class(*args: object, **kwargs: object) -> None:
    raise type(LEAKY_NAME, (RuntimeError,), {})(SENTINEL)


def _code_block() -> Block:
    return CodeBlock(label="code_1", code="return 1", output_parameter=_output_parameter())


def _loop_block() -> Block:
    return ForLoopBlock(label="loop_1", output_parameter=_output_parameter(), loop_over=None, loop_blocks=[])


@pytest.mark.parametrize(
    ("make_block", "target", "raiser", "expected_reason", "expected_class", "expected_line", "withheld_name"),
    [
        (
            _code_block,
            (CodeBlock, "_execute"),
            _raise_from_user_code_frame,
            f"CodeBlock failed with KeyError inside Skyvern while running line {USER_CODE_LINE}.",
            "builtins.KeyError",
            USER_CODE_LINE,
            None,
        ),
        (
            _code_block,
            (CodeBlock, "_execute"),
            _raise_user_defined,
            "CodeBlock failed inside Skyvern.",
            None,
            None,
            "SecretError",
        ),
        (
            _loop_block,
            (ForLoopBlock, "execute"),
            _raise_index_error,
            "Loop block failed with IndexError.",
            "builtins.IndexError",
            None,
            None,
        ),
        (
            _code_block,
            (CodeBlock, "_execute"),
            _raise_unbound_class,
            "CodeBlock failed inside Skyvern.",
            None,
            None,
            LEAKY_NAME,
        ),
    ],
    ids=["builtin-with-user-code-frame", "user-defined-class-withheld", "loop-block", "unbound-class-withheld"],
)
@pytest.mark.asyncio
async def test_machinery_failure_reason_and_log_fields(
    make_block, target, raiser, expected_reason, expected_class, expected_line, withheld_name
) -> None:
    block = make_block()
    with (
        patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        patch.object(*target, new=raiser),
        patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
        patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        capture_logs() as logs,
    ):
        _setup_mocks(mock_app)
        mock_app.AGENT_FUNCTION.prepare_codeblock_control_flow_exception.side_effect = (
            AgentFunction().prepare_codeblock_control_flow_exception
        )
        mock_app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled.return_value = False
        mock_app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts.return_value = []

        result = await block.execute_safe(workflow_run_id="wr_1")

    assert result.success is False
    assert result.failure_reason == expected_reason

    events = [e for e in logs if e["event"].endswith("execution failed")]
    assert len(events) == 1
    event = events[0]
    assert "exc_info" not in event
    assert event["exception_class"] == expected_class
    assert event["failing_line"] == expected_line
    assert event["stack"]
    if expected_line is not None:
        assert f"{CODE_BLOCK_FILENAME}:{expected_line}" in event["stack"]
        assert any("block.py" in frame for frame in event["stack"])

    leaked = repr(logs) + result.failure_reason
    assert SENTINEL not in leaked
    assert USER_SOURCE_MARKER not in leaked
    if withheld_name:
        assert withheld_name not in leaked
