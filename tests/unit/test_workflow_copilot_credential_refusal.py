"""Deterministic prompt + tool-docstring tests for the v2 raw-credential refusal.

Locks in the fix for SKY-9189. The rule lives in two places the agent reads
as operating instructions: the system prompt template
`workflow-copilot-agent.j2`, and the tool docstrings for
`run_blocks_and_collect_debug` and `update_and_run_blocks` (exposed to the
agents SDK via FunctionTool.description). Both must state the same policy,
or the agent follows whichever it weights higher.
"""

from skyvern.forge.sdk.copilot.tools import (
    add_block_tool,
    edit_block_and_run_tool,
    fill_credential_field_tool,
    run_blocks_tool,
    update_and_run_blocks_tool,
)
from tests.unit.conftest import render_agent_prompt as _render_agent_prompt


class TestAgentPromptRefusalClause:
    """The rewritten system prompt must carry the hard refusal rule."""

    def test_raw_secret_is_not_echoed_used_or_run(self) -> None:
        rendered = _render_agent_prompt()
        assert "do not echo it" in rendered
        assert "do not type or submit it into a page" in rendered
        assert "do not pass it as a run parameter" in rendered
        assert "do not use the browser or run anything with it" in rendered
        assert "persist only a redacted draft that uses a saved credential parameter" in rendered

    def test_saved_credentials_are_resolved_by_name_or_id(self) -> None:
        rendered = _render_agent_prompt()
        assert "resolve one the user names, by exact name or credential ID" in rendered
        assert "When a credential has already been resolved for the page you are on, use it" in rendered

    def test_old_permissive_clause_is_gone(self) -> None:
        """The v2 template used to authorize inline secrets via `parameters` — must be removed."""
        rendered = _render_agent_prompt()
        assert "redacted from the outbound client stream" not in rendered
        assert "you may pass it via `parameters`" not in rendered


class TestToolDocstringsRefusalClause:
    """Tool docstrings reach the agent via FunctionTool.description — they must agree with the prompt."""

    def _tools(self) -> list[object]:
        return [run_blocks_tool, update_and_run_blocks_tool, edit_block_and_run_tool]

    def test_old_permissive_clause_gone_from_tools(self) -> None:
        """The clause that told the agent inline secrets were fine via `parameters` is removed."""
        for tool in self._tools():
            desc = tool.description  # type: ignore[attr-defined]
            assert "redacted from" not in desc, f"{tool.name} still claims redaction"  # type: ignore[attr-defined]
            assert "you may pass it via" not in desc, f"{tool.name} still permits inline secrets"  # type: ignore[attr-defined]

    def test_tools_state_the_refusal_without_a_dangling_prompt_reference(self) -> None:
        for tool in self._tools():
            desc = tool.description  # type: ignore[attr-defined]
            assert "do NOT pass" in desc, f"{tool.name} does not forbid inline secret pass-through"  # type: ignore[attr-defined]
            assert "Ask the user to store it as a saved" in desc
            assert "credential and reply with the credential name" in desc
            assert "do not build or run with" in desc
            assert "the raw value" in desc
            assert "CREDENTIAL HANDLING refusal rule" not in desc

    def test_non_secret_parameters_guidance_preserved(self) -> None:
        """The `parameters` dict is still the right channel for non-secret runtime values."""
        for tool in self._tools():
            desc = tool.description  # type: ignore[attr-defined]
            assert "non-secret values" in desc, f"{tool.name} missing non-secret guidance"  # type: ignore[attr-defined]

    def test_list_credentials_tool_describes_pagination(self) -> None:
        """list_credentials docstring must warn about paging before concluding no match."""
        from skyvern.forge.sdk.copilot.tools import list_credentials_tool

        desc = list_credentials_tool.description  # type: ignore[attr-defined]
        assert "has_more" in desc
        assert "already stored on a later page" in desc

    def test_saved_login_state_is_reused_instead_of_rescouted(self) -> None:
        run_desc = " ".join(run_blocks_tool.description.split())  # type: ignore[attr-defined]
        fill_desc = " ".join(fill_credential_field_tool.description.split()).lower()  # type: ignore[attr-defined]

        assert "existing saved block" in run_desc
        assert "run that block unchanged" in run_desc
        assert "existing saved login block" in fill_desc
        assert "run that block unchanged" in fill_desc

    def test_add_block_describes_the_flat_workflow_parameter_shape(self) -> None:
        desc = add_block_tool.description  # type: ignore[attr-defined]

        assert '"parameter_type": "workflow"' in desc
        assert '"workflow_parameter_type": "string"' in desc
        assert '"default_value": "BillingHistory.jsp"' in desc
        assert "Inspect or run the saved workflow" in desc


class TestBrowserToolOverlayRefusalCaveat:
    """The MCP browser-tool overlays are also operating instructions for the agent.

    type_text is the primary leakage vector (typing a pasted password into a
    live login form). Its overlay description must tell the agent to refuse
    there too — otherwise prompt-level refusal is bypassable by the agent
    following the overlay's own (previously silent) description.
    """

    def test_type_text_overlay_forbids_inline_secrets(self) -> None:
        from skyvern.forge.sdk.copilot.tools import _build_skyvern_mcp_overlays

        overlays = _build_skyvern_mcp_overlays()
        assert "type_text" in overlays
        desc = overlays["type_text"].description or ""
        assert "NEVER type inline passwords" in desc
        assert "Ask the user to store the value as a saved credential" in desc
        assert "do not type or submit the raw value" in desc
        assert "CREDENTIAL HANDLING refusal rule" not in desc
