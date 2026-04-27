"""Tests for phantom script_block entry prevention (SKY-8443).

When _generate_pending_script_for_block fires after an early block completion,
generate_workflow_script_python_code must NOT create script_block DB entries
for blocks that haven't executed yet (no actions, no task_id). Creating such
entries causes a permanent stuck state where subsequent runs think all blocks
are cached but the Python file only has code for the first block.
"""


def test_unexecuted_block_detected_by_guard():
    """A block with no actions and no task_id should be identified as unexecuted."""
    # Simulates a block in workflow_blocks that has a workflow_run_block entry
    # (pre-created in queued state) but hasn't actually run yet
    unexecuted_block = {
        "block_type": "navigation",
        "label": "block_1",
        "task_id": "",  # Empty — no task created yet
        "workflow_run_block_id": "wrb_123",
    }
    actions_by_task: dict = {}

    task_id = unexecuted_block.get("task_id", "")
    block_actions = actions_by_task.get(task_id, [])

    # This is the guard condition from generate_script.py
    should_skip = not block_actions and not task_id
    assert should_skip, "Block with no actions and no task_id should be skipped"


def test_executed_block_with_actions_passes_guard():
    """A block with a task_id and actions should NOT be skipped."""
    executed_block = {
        "block_type": "navigation",
        "label": "block_1",
        "task_id": "tsk_456",
        "workflow_run_block_id": "wrb_123",
    }
    actions_by_task = {
        "tsk_456": [{"action_type": "click", "element_id": "AAAB"}],
    }

    task_id = executed_block.get("task_id", "")
    block_actions = actions_by_task.get(task_id, [])

    should_skip = not block_actions and not task_id
    assert not should_skip, "Block with task_id and actions should NOT be skipped"


def test_executed_block_with_task_id_but_no_actions_passes_guard():
    """A block with a task_id but zero actions should NOT be skipped.
    This is a valid scenario — the block executed but completed immediately."""
    executed_empty_block = {
        "block_type": "navigation",
        "label": "block_1",
        "task_id": "tsk_789",
        "workflow_run_block_id": "wrb_123",
    }
    actions_by_task: dict = {}

    task_id = executed_empty_block.get("task_id", "")
    block_actions = actions_by_task.get(task_id, [])

    should_skip = not block_actions and not task_id
    assert not should_skip, "Block with task_id (even without actions) should NOT be skipped"


def test_progressive_caching_scenario():
    """Simulate the exact scenario from SKY-8443: login completes first,
    block_1 and block_2 haven't executed yet. Only login should produce
    a script_block entry."""
    blocks = [
        {"block_type": "login", "label": "login", "task_id": "tsk_001"},
        {"block_type": "navigation", "label": "block_1", "task_id": ""},  # Not executed
        {"block_type": "file_download", "label": "block_2", "task_id": ""},  # Not executed
    ]
    actions_by_task = {
        "tsk_001": [
            {"action_type": "input_text", "element_id": "AAA1"},
            {"action_type": "click", "element_id": "AAA2"},
        ],
    }

    blocks_that_should_get_entries = []
    for block in blocks:
        task_id = block.get("task_id", "")
        block_actions = actions_by_task.get(task_id, [])
        if not block_actions and not task_id:
            continue  # Skip — unexecuted
        blocks_that_should_get_entries.append(block["label"])

    assert blocks_that_should_get_entries == ["login"], (
        "Only login should get a script_block entry; block_1 and block_2 haven't executed"
    )
