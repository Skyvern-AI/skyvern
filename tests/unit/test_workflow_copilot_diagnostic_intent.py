"""Prompt and tool-description guards for diagnostic copilot turns."""

import re

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


class TestDiagnosticAbsentEntity:
    """Pin SKY-9756: diagnostic complaints naming an absent workflow entity must not get a workflow-knowledge-base explanation in isolation."""

    def test_diagnostic_section_requires_entity_presence_check(self) -> None:
        rendered = _render_agent_prompt()

        for phrase in (
            "block label, workflow parameter key, or `{{ name.output... }}` Jinja reference",
            "verify the entity is present in the current workflow YAML",
            "The current workflow is the source of truth",
            "Pending build with a plausible block sequence",
            "Pending build still waiting on user input",
            "No pending build in chat history",
            "fall back to the CRITICAL ROLE BOUNDARY docs-inline path",
            "Do not fabricate a workflow",
        ):
            assert phrase in rendered, f"missing diagnostic absent-entity phrase: {phrase!r}"

    def test_step_5_spells_out_knowledge_base_does_not_use_kb_acronym(self) -> None:
        # The rest of the prompt spells out "WORKFLOW KNOWLEDGE BASE"; the new
        # step 5 must match that convention so the LLM has no ambiguity about
        # what "KB" would have meant.
        rendered = _render_agent_prompt()
        section_marker = "DIAGNOSTIC / OBSERVATIONAL COMPLAINTS:"
        end_marker = "Explicit edit/debug requests remain edit requests"
        start = rendered.index(section_marker)
        end = rendered.index(end_marker, start)
        section = rendered[start:end]
        assert "workflow-knowledge-base explanation" in section
        assert "workflow-knowledge-base prose" in section
        assert not re.search(r"\bKB\b", section), "step 5 must spell out 'knowledge base', not abbreviate to 'KB'"

    def test_docs_inline_disambiguator_defers_to_diagnostic_step_5(self) -> None:
        rendered = _render_agent_prompt()

        assert "Docs-inline does NOT apply to a runtime symptom" in rendered
        assert "a block label, workflow parameter key, or `{{ name.output... }}` Jinja reference" in rendered
        assert "DIAGNOSTIC / OBSERVATIONAL COMPLAINTS step 5" in rendered

    def test_diagnostic_step_5_is_actually_numbered_in_section(self) -> None:
        rendered = _render_agent_prompt()
        section_marker = "DIAGNOSTIC / OBSERVATIONAL COMPLAINTS:"
        end_marker = "Explicit edit/debug requests remain edit requests"
        start = rendered.index(section_marker)
        end = rendered.index(end_marker, start)
        section = rendered[start:end]
        assert "\n5. " in section, "step 5 missing from DIAGNOSTIC / OBSERVATIONAL COMPLAINTS numbered list"
