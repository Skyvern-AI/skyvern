"""Tool-contract guards for diagnostic copilot turns."""

from skyvern.forge.sdk.copilot.tools import run_blocks_tool, update_and_run_blocks_tool


class TestDiagnosticObservationIntent:
    """Pin diagnostics to self-contained prompt and tool behavior."""

    def test_block_running_tools_state_diagnostic_routing_directly(self) -> None:
        for tool in (run_blocks_tool, update_and_run_blocks_tool):
            desc = tool.description  # type: ignore[attr-defined]
            assert "diagnostic complaints" in desc
            assert "ASK-vs-EDIT routing" not in desc

        run_desc = run_blocks_tool.description  # type: ignore[attr-defined]
        assert "no prior edit goal" in run_desc
        assert "`update_and_run_blocks` instead of rerunning unchanged blocks" in run_desc
        assert "instead of rerunning unchanged blocks" in run_desc

        update_desc = update_and_run_blocks_tool.description  # type: ignore[attr-defined]
        assert "diagnostic follow-up after an explicit" in update_desc
        assert "edit goal may update and run once the correction is clear" in update_desc
