"""
Unit tests for WorkflowTriggerBlock support in cached scripts (SKY-8575).

WorkflowTriggerBlock makes zero LLM calls — it's pure orchestration
(template resolution, workflow dispatch, output collection). These tests
verify it generates valid code for cached script execution, especially
inside ForLoop blocks where the original bug manifested.
"""

import ast

import libcst as cst

from skyvern.core.script_generations.generate_script import (
    _build_for_loop_statement,
    _build_workflow_trigger_statement,
)


class TestWorkflowTriggerStatement:
    """Test that _build_workflow_trigger_statement generates valid skyvern.trigger_workflow() calls."""

    def test_basic_trigger_generates_await_call(self) -> None:
        """Verify basic trigger block generates await skyvern.trigger_workflow(...)."""
        block = {
            "block_type": "workflow_trigger",
            "label": "trigger_child",
            "workflow_permanent_id": "wpid_123456",
        }

        result = _build_workflow_trigger_statement(block)
        code = cst.Module(body=[result]).code

        assert "await skyvern.trigger_workflow" in code
        assert "workflow_permanent_id" in code
        assert "wpid_123456" in code
        assert "label" in code
        assert "trigger_child" in code

    def test_trigger_with_payload(self) -> None:
        """Verify payload dict is included in generated code."""
        block = {
            "block_type": "workflow_trigger",
            "label": "trigger_with_data",
            "workflow_permanent_id": "wpid_999",
            "payload": {"zip_code": "90210", "state": "CA"},
        }

        result = _build_workflow_trigger_statement(block)
        code = cst.Module(body=[result]).code

        assert "payload" in code
        assert "zip_code" in code
        assert "90210" in code

    def test_trigger_with_wait_for_completion(self) -> None:
        """Verify wait_for_completion flag is included."""
        block = {
            "block_type": "workflow_trigger",
            "label": "sync_trigger",
            "workflow_permanent_id": "wpid_sync",
            "wait_for_completion": True,
        }

        result = _build_workflow_trigger_statement(block)
        code = cst.Module(body=[result]).code

        assert "wait_for_completion" in code

    def test_trigger_async_fire_and_forget(self) -> None:
        """Verify async trigger generates wait_for_completion=False."""
        block = {
            "block_type": "workflow_trigger",
            "label": "async_trigger",
            "workflow_permanent_id": "wpid_async",
            "wait_for_completion": False,
        }

        result = _build_workflow_trigger_statement(block)
        code = cst.Module(body=[result]).code

        assert "wait_for_completion = False" in code or "wait_for_completion=False" in code

    def test_trigger_with_parent_browser_session(self) -> None:
        """Verify use_parent_browser_session is included when True."""
        block = {
            "block_type": "workflow_trigger",
            "label": "shared_browser",
            "workflow_permanent_id": "wpid_browser",
            "use_parent_browser_session": True,
        }

        result = _build_workflow_trigger_statement(block)
        code = cst.Module(body=[result]).code

        assert "use_parent_browser_session" in code

    def test_trigger_without_parent_browser_session_omits_it(self) -> None:
        """When use_parent_browser_session is False, it should be omitted."""
        block = {
            "block_type": "workflow_trigger",
            "label": "no_shared_browser",
            "workflow_permanent_id": "wpid_no_browser",
            "use_parent_browser_session": False,
        }

        result = _build_workflow_trigger_statement(block)
        code = cst.Module(body=[result]).code

        assert "use_parent_browser_session" not in code

    def test_trigger_with_explicit_browser_session_id(self) -> None:
        """Verify explicit browser_session_id is included."""
        block = {
            "block_type": "workflow_trigger",
            "label": "explicit_session",
            "workflow_permanent_id": "wpid_session",
            "browser_session_id": "pbs_12345",
        }

        result = _build_workflow_trigger_statement(block)
        code = cst.Module(body=[result]).code

        assert "browser_session_id" in code
        assert "pbs_12345" in code

    def test_trigger_compiles_as_valid_python(self) -> None:
        """Verify the generated statement is syntactically valid Python."""
        block = {
            "block_type": "workflow_trigger",
            "label": "compile_test",
            "workflow_permanent_id": "wpid_compile",
            "payload": {"key": "value"},
            "wait_for_completion": True,
            "use_parent_browser_session": True,
        }

        result = _build_workflow_trigger_statement(block)
        code = cst.Module(body=[result]).code

        # Wrap in async function so the await is valid
        wrapped = "async def _test():\n" + "\n".join(f"    {line}" for line in code.strip().splitlines())
        ast.parse(wrapped)  # Should not raise SyntaxError


class TestWorkflowTriggerInsideForLoop:
    """Test the key bug scenario: workflow_trigger inside a for_loop (SKY-8575)."""

    def test_forloop_with_trigger_block_generates_valid_code(self) -> None:
        """A ForLoop containing a workflow_trigger block should produce a valid async for
        statement with the trigger call in its body — not a no-op comment."""
        forloop_block = {
            "block_type": "for_loop",
            "label": "iterate_zip_codes",
            "loop_variable_reference": "{{ zip_codes }}",
            "loop_blocks": [
                {
                    "block_type": "workflow_trigger",
                    "label": "trigger_per_zip",
                    "workflow_permanent_id": "wpid_child_workflow",
                    "payload": {"zip": "{{ current_value }}"},
                    "wait_for_completion": True,
                },
            ],
        }

        result = _build_for_loop_statement("iterate_zip_codes", forloop_block)
        code = cst.Module(body=[result]).code

        # The loop body should contain the trigger call, not 'Unknown block type'
        assert "skyvern.trigger_workflow" in code
        assert "Unknown block type" not in code
        assert "wpid_child_workflow" in code

    def test_forloop_with_trigger_and_extraction_generates_both(self) -> None:
        """A ForLoop with both a trigger and extraction block should include both in the body."""
        forloop_block = {
            "block_type": "for_loop",
            "label": "multi_block_loop",
            "loop_variable_reference": "{{ items }}",
            "loop_blocks": [
                {
                    "block_type": "workflow_trigger",
                    "label": "trigger_step",
                    "workflow_permanent_id": "wpid_target",
                    "wait_for_completion": True,
                },
                {
                    "block_type": "extraction",
                    "label": "extract_step",
                    "data_extraction_goal": "Get result",
                    "task_id": "task_001",
                },
            ],
        }

        result = _build_for_loop_statement("multi_block_loop", forloop_block)
        code = cst.Module(body=[result]).code

        # Both blocks should appear in the loop body
        assert "skyvern.trigger_workflow" in code
        assert "extract_step" in code

    def test_forloop_with_trigger_compiles(self) -> None:
        """Full compilation test: ForLoop + trigger block generates valid Python."""
        forloop_block = {
            "block_type": "for_loop",
            "label": "compile_loop",
            "loop_variable_reference": "{{ data }}",
            "loop_blocks": [
                {
                    "block_type": "workflow_trigger",
                    "label": "child_trigger",
                    "workflow_permanent_id": "wpid_test",
                },
            ],
        }

        result = _build_for_loop_statement("compile_loop", forloop_block)
        code = cst.Module(body=[result]).code

        # Wrap in an async function for valid Python
        wrapped = "async def _test():\n" + "\n".join(f"    {line}" for line in code.strip().splitlines())
        ast.parse(wrapped)  # Should not raise SyntaxError
