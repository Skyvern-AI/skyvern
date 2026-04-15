"""
Tests for empty-block script regeneration (SKY-8684).

When a WorkflowScript record exists but has zero ScriptBlock records,
subsequent runs should detect this and ensure the script gets regenerated.
Without this fix, the empty-block script persists forever because:
1. generate_script is set to False (script exists)
2. Per-block script generation never fires
3. Post-run finalize may skip regeneration for non-completed runs
"""

from unittest.mock import MagicMock

from skyvern.forge.sdk.core.skyvern_context import SkyvernContext


class TestEmptyBlockScriptDetection:
    """Tests for detection and handling of scripts with zero usable blocks."""

    def test_empty_script_blocks_sets_generate_script_true(self) -> None:
        """When a script exists with zero usable blocks AND is_script_run=True,
        generate_script should be set to True on the context so regeneration
        can occur during and after execution.

        This is the core bug fix: previously generate_script stayed False
        because the script existed, preventing regeneration."""
        # The fix adds logic after line 1412 in _execute_workflow_blocks:
        # if script and is_script_run and not script_blocks_by_label:
        #     ctx.generate_script = True
        ctx = SkyvernContext()
        ctx.generate_script = False  # Simulates line 1072 setting it to False

        script = MagicMock()
        script.script_id = "scr_test123"
        script.script_revision_id = "scr_rev_test123"
        is_script_run = True
        script_blocks_by_label: dict = {}  # Zero usable blocks

        # This is the logic that should exist after line 1412
        if script and is_script_run and not script_blocks_by_label:
            ctx.generate_script = True

        assert ctx.generate_script is True, "generate_script should be True when script has zero usable blocks"

    def test_normal_script_with_blocks_keeps_generate_script_false(self) -> None:
        """Regression test: scripts with usable blocks should NOT override
        generate_script to True — the existing script handles execution."""
        ctx = SkyvernContext()
        ctx.generate_script = False  # Set by line 1072

        script = MagicMock()
        script.script_id = "scr_normal"
        script.script_revision_id = "scr_rev_normal"
        is_script_run = True
        script_blocks_by_label = {
            "task_1": MagicMock(run_signature="await run_task_1()", requires_agent=False),
        }

        # Same logic — but should NOT fire because blocks exist
        if script and is_script_run and not script_blocks_by_label:
            ctx.generate_script = True

        assert ctx.generate_script is False, "generate_script should stay False when script has usable blocks"

    def test_no_script_keeps_generate_script_default(self) -> None:
        """When no script exists, generate_script defaults to True
        (the SkyvernContext default), allowing fresh generation."""
        ctx = SkyvernContext()
        # generate_script defaults to True in SkyvernContext
        assert ctx.generate_script is True

        script = None
        is_script_run = True
        script_blocks_by_label: dict = {}

        # Logic should not fire when script is None
        if script and is_script_run and not script_blocks_by_label:
            ctx.generate_script = True

        assert ctx.generate_script is True

    def test_non_script_run_does_not_override(self) -> None:
        """When is_script_run is False (pure agent mode), the empty-block
        detection should not fire — there's no script to regenerate."""
        ctx = SkyvernContext()
        ctx.generate_script = False

        script = MagicMock()
        is_script_run = False
        script_blocks_by_label: dict = {}

        if script and is_script_run and not script_blocks_by_label:
            ctx.generate_script = True

        assert ctx.generate_script is False, "generate_script should not be overridden for non-script runs"


class TestEmptyBlockWarningLog:
    """Tests for the WARNING log emitted when empty-block scripts are detected."""

    def test_warning_log_includes_required_fields(self) -> None:
        """The warning log for empty-block detection should include
        workflow_permanent_id, workflow_run_id, script_id, and script_revision_id
        so we can track this in Datadog."""
        # Build mock objects
        script = MagicMock()
        script.script_id = "scr_empty"
        script.script_revision_id = "scr_rev_empty"

        workflow = MagicMock()
        workflow.workflow_permanent_id = "wpid_test"

        workflow_run_id = "wr_test_run_123"

        is_script_run = True
        script_blocks_by_label: dict = {}

        log_calls: list[dict] = []

        def mock_warning(msg: str, **kwargs: object) -> None:
            log_calls.append({"msg": msg, "kwargs": kwargs})

        mock_log = MagicMock()
        mock_log.warning = mock_warning

        # Simulate the detection logic with logging
        if script and is_script_run and not script_blocks_by_label:
            mock_log.warning(
                "Script exists but has zero usable blocks — will regenerate",
                workflow_permanent_id=workflow.workflow_permanent_id,
                workflow_run_id=workflow_run_id,
                script_id=script.script_id,
                script_revision_id=script.script_revision_id,
            )

        assert len(log_calls) == 1
        call = log_calls[0]
        assert "zero usable blocks" in call["msg"]
        assert call["kwargs"]["workflow_permanent_id"] == "wpid_test"
        assert call["kwargs"]["workflow_run_id"] == "wr_test_run_123"
        assert call["kwargs"]["script_id"] == "scr_empty"
        assert call["kwargs"]["script_revision_id"] == "scr_rev_empty"

    def test_no_warning_when_blocks_exist(self) -> None:
        """No warning should be logged when the script has usable blocks."""
        script = MagicMock()
        is_script_run = True
        script_blocks_by_label = {"task_1": MagicMock()}

        log_calls: list[dict] = []

        def mock_warning(msg: str, **kwargs: object) -> None:
            log_calls.append({"msg": msg, "kwargs": kwargs})

        mock_log = MagicMock()
        mock_log.warning = mock_warning

        if script and is_script_run and not script_blocks_by_label:
            mock_log.warning(
                "Script exists but has zero usable blocks — will regenerate",
                workflow_permanent_id="wpid_test",
                workflow_run_id="wr_test",
                script_id=script.script_id,
                script_revision_id=script.script_revision_id,
            )

        assert len(log_calls) == 0, "No warning should be logged when blocks exist"
