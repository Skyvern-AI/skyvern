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
    """Pin diagnostic complaint routing to the consolidated ASK-vs-EDIT rule."""

    def test_agent_prompt_uses_consolidated_ask_vs_edit_rule(self) -> None:
        rendered = _render_agent_prompt()

        assert "ASK-vs-EDIT ROUTING:" in rendered
        assert "Use one decision tree for the latest turn" in rendered
        assert "DIAGNOSTIC / OBSERVATIONAL COMPLAINTS" not in rendered
        assert "Explicit edit/debug requests remain edit requests" not in rendered

    def test_block_running_tools_share_consolidated_diagnostic_routing(self) -> None:
        for tool in (run_blocks_tool, update_and_run_blocks_tool):
            desc = tool.description  # type: ignore[attr-defined]
            assert "diagnostic complaints" in desc
            assert "ASK-vs-EDIT routing" in desc

        run_desc = run_blocks_tool.description  # type: ignore[attr-defined]
        assert "no prior edit goal" in run_desc
        assert "use `update_and_run_blocks`" in run_desc
        assert "instead of rerunning unchanged blocks" in run_desc

        update_desc = update_and_run_blocks_tool.description  # type: ignore[attr-defined]
        assert "diagnostic follow-up after an explicit edit goal" in update_desc
        assert "update/run once the correction is clear" in update_desc


class TestDiagnosticAbsentEntity:
    """Pin SKY-9756 absent-entity symptoms to workflow state before docs prose."""

    def test_ask_vs_edit_routing_requires_entity_presence_check(self) -> None:
        rendered = _render_agent_prompt()

        for phrase in (
            "block label, workflow parameter key, or `{{ name.output... }}` Jinja reference",
            "is present in the current workflow YAML",
            "The current workflow is the source of truth",
            "chat history contains a plausible pending build/edit",
            "create or correct the real workflow blocks and call `update_and_run_blocks`",
            "respond with `ASK_QUESTION` naming both the absent entity and missing requirement",
            "Only use docs-inline when no pending workflow build/edit remains",
        ):
            assert phrase in rendered, f"missing absent-entity routing phrase: {phrase!r}"

    def test_ask_vs_edit_spells_out_knowledge_base_does_not_use_kb_acronym(self) -> None:
        # The rest of the prompt spells out "WORKFLOW KNOWLEDGE BASE"; the
        # routing rule should not introduce a new "KB" abbreviation.
        rendered = _render_agent_prompt()
        section_marker = "ASK-vs-EDIT ROUTING:"
        end_marker = "WORKFLOW-FIRST EXECUTION PATH"
        start = rendered.index(section_marker)
        end = rendered.index(end_marker, start)
        section = rendered[start:end]
        assert "workflow-knowledge-base prose" in section
        assert "KB" not in section
