"""
Test that block output parameters are correctly scoped across loop iterations.

Verifies that when the same block runs multiple times inside a for-loop,
later iterations' extracted_information takes precedence over earlier ones.
"""

from datetime import datetime

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType


def _make_output_parameter(key: str) -> OutputParameter:
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        output_parameter_id="op_test",
        workflow_id="wf_test",
        created_at=datetime.now(),
        modified_at=datetime.now(),
    )


def test_block_output_updates_across_loop_iterations():
    """
    Simulates two loop iterations where block 'extract_data' produces different
    extracted_information each time. Verifies that the second registration
    overwrites (not merges-under) the first.
    """
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=None,  # type: ignore[arg-type]
    )

    param = _make_output_parameter("extract_data_output")

    # --- Iteration 1 ---
    iteration_1_value = {
        "extracted_information": {"quote": "Quote from page 1", "author": "Author 1"},
        "status": "completed",
    }
    ctx.register_block_reference_variable_from_output_parameter(param, iteration_1_value)

    assert ctx.values["extract_data"]["extracted_information"] == {
        "quote": "Quote from page 1",
        "author": "Author 1",
    }

    # --- Iteration 2 ---
    iteration_2_value = {
        "extracted_information": {"quote": "Quote from page 2", "author": "Author 2"},
        "status": "completed",
    }
    ctx.register_block_reference_variable_from_output_parameter(param, iteration_2_value)

    # After iteration 2, values must reflect iteration 2's data
    result = ctx.values["extract_data"]
    assert result["extracted_information"] == {"quote": "Quote from page 2", "author": "Author 2"}, (
        f"Iteration 2's extracted_information was overwritten by iteration 1's. Got: {result}"
    )
    # The `output` alias must also reflect the latest iteration
    assert result["output"] == {"quote": "Quote from page 2", "author": "Author 2"}


def test_old_only_keys_preserved_across_iterations():
    """
    When iteration 1 produces keys that iteration 2 does not, those keys
    should be preserved (merge semantics), while overlapping keys use
    iteration 2's values.
    """
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=None,  # type: ignore[arg-type]
    )

    param = _make_output_parameter("block_output")

    # Iteration 1 has an extra key "extra_field"
    ctx.register_block_reference_variable_from_output_parameter(
        param,
        {
            "extracted_information": {"name": "Alice"},
            "extra_field": "only_in_iter1",
            "status": "completed",
        },
    )

    # Iteration 2 does not have "extra_field"
    ctx.register_block_reference_variable_from_output_parameter(
        param,
        {
            "extracted_information": {"name": "Bob"},
            "status": "completed",
        },
    )

    result = ctx.values["block"]
    # Overlapping keys use iteration 2's values
    assert result["extracted_information"] == {"name": "Bob"}
    assert result["status"] == "completed"
    # Old-only keys are preserved
    assert result["extra_field"] == "only_in_iter1"
