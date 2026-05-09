"""Prompt and tool-description guards for diagnostic copilot turns."""

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.tools import run_blocks_tool, update_and_run_blocks_tool

_AGENT_TEMPLATE_DEFAULTS = dict(
    workflow_knowledge_base="test kb",
    current_datetime="2026-01-01T00:00:00Z",
    tool_usage_guide="",
    security_rules="",
)


def _render_agent_prompt() -> str:
    return prompt_engine.load_prompt("workflow-copilot-agent", **_AGENT_TEMPLATE_DEFAULTS)


class TestDiagnosticObservationIntent:
    """Pin SKY-9651: observational complaints inspect first instead of edit/run by default."""

    def test_agent_prompt_has_diagnostic_observation_section(self) -> None:
        rendered = _render_agent_prompt()

        assert "DIAGNOSTIC / OBSERVATIONAL COMPLAINTS" in rendered
        assert "inspect-and-clarify" in rendered
        assert "Do NOT call `update_and_run_blocks`" in rendered
        assert "do NOT call `run_blocks_and_collect_debug`" in rendered

    def test_agent_prompt_names_failed_repro_shapes(self) -> None:
        rendered = _render_agent_prompt()

        for phrase in (
            "missing newly added workflow block",
            "not following a configured search step",
            "whether a named block is present",
            "runtime error condition did not trigger",
            "missing or unknown block labels",
        ):
            assert phrase in rendered

    def test_agent_prompt_preserves_explicit_edit_requests(self) -> None:
        rendered = _render_agent_prompt()

        assert "Explicit edit/debug requests remain edit requests" in rendered
        assert "how can I improve" in rendered
        assert "what would fix" in rendered
        assert "call `update_and_run_blocks`" in rendered

    def test_block_running_tools_defer_diagnostic_complaints(self) -> None:
        for tool in (run_blocks_tool, update_and_run_blocks_tool):
            desc = tool.description  # type: ignore[attr-defined]
            assert "diagnostic / observational complaint" in desc
            assert "inspect-and-clarify" in desc
            assert "not the first response" in desc
