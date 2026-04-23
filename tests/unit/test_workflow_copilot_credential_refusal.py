"""Deterministic prompt + tool-docstring tests for the v2 raw-credential refusal.

Locks in the fix for SKY-9189. The rule lives in two places the agent reads
as operating instructions: the system prompt template
`workflow-copilot-agent.j2`, and the tool docstrings for
`run_blocks_and_collect_debug` and `update_and_run_blocks` (exposed to the
agents SDK via FunctionTool.description). Both must state the same policy,
or the agent follows whichever it weights higher.
"""

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.tools import (
    run_blocks_tool,
    update_and_run_blocks_tool,
)

_AGENT_TEMPLATE_DEFAULTS = dict(
    workflow_knowledge_base="test kb",
    current_datetime="2026-01-01T00:00:00Z",
    tool_usage_guide="",
    security_rules="",
)


def _render_agent_prompt() -> str:
    return prompt_engine.load_prompt("workflow-copilot-agent", **_AGENT_TEMPLATE_DEFAULTS)


class TestAgentPromptRefusalClause:
    """The rewritten system prompt must carry the hard refusal rule."""

    def test_uppercase_marker_present(self) -> None:
        """The recognizability marker preserved from v1 must render verbatim."""
        rendered = _render_agent_prompt()
        assert "DO NOT PROVIDE RAW LOGIN/PASSWORD" in rendered

    def test_critical_section_header_present(self) -> None:
        """The section is labelled CRITICAL so prompt scanners can find it."""
        rendered = _render_agent_prompt()
        assert "CREDENTIAL HANDLING - CRITICAL" in rendered

    def test_ask_question_response_required(self) -> None:
        """On refusal, the agent must use ASK_QUESTION, not a workflow build."""
        rendered = _render_agent_prompt()
        assert "ASK_QUESTION" in rendered
        assert "MUST NOT build, update, or run a workflow" in rendered

    def test_generalized_secret_surface_enumerated(self) -> None:
        """Refusal covers more than username/password — all the common leakage shapes."""
        rendered = _render_agent_prompt()
        for term in (
            "API keys",
            "OAuth tokens",
            "Authorization: Bearer",
            "JWTs",
            "session cookies",
            "TOTP seeds",
            "OTP",
            "private keys",
            "credit card numbers",
            "CVVs",
        ):
            assert term in rendered, f"missing secret-surface term: {term}"

    def test_browser_tools_also_forbidden(self) -> None:
        """The refusal must block not just workflow build/run but direct browser-tool paths too.

        Without this, an agent can 'comply' by skipping workflow tools and still
        type_text the pasted password into a live login form. Explicit names of
        the browser tools keep the rule concrete for the LLM.
        """
        rendered = _render_agent_prompt()
        assert "type_text" in rendered
        assert "click" in rendered
        assert "press_key" in rendered
        assert "any other direct browser tool" in rendered

    def test_list_credentials_pagination_instruction(self) -> None:
        """Prompt must tell the agent to page list_credentials before concluding 'no match'.

        list_credentials defaults to page_size=10 and caps at 50. Without this
        instruction the agent can falsely tell the user 'no stored credential
        exists' and send them to the UI to create one they already have.
        """
        rendered = _render_agent_prompt()
        assert "page_size=50" in rendered
        assert "has_more" in rendered

    def test_negative_bare_identifier_is_not_a_credential(self) -> None:
        """A bare username/email alone must not trigger the refusal rule.

        The section explicitly calls out that bare non-secret identifiers don't
        count, so the prompt doesn't over-refuse ('my username is alice').
        """
        rendered = _render_agent_prompt()
        assert "bare username" in rendered
        assert "NOT a raw credential" in rendered
        assert "does not trigger this rule" in rendered

    def test_list_credentials_called_first(self) -> None:
        """The refusal flow routes through list_credentials for discoverable matches."""
        rendered = _render_agent_prompt()
        assert "FIRST call `list_credentials`" in rendered

    def test_no_domain_matching(self) -> None:
        """list_credentials has no domain field, so the rule must match on name/username only."""
        rendered = _render_agent_prompt()
        assert "no site/domain field" in rendered
        assert "on `name` / `username` only" in rendered

    def test_no_raw_secret_echoed(self) -> None:
        """The agent must not echo the raw secret into its reply or persistent context."""
        rendered = _render_agent_prompt()
        assert "Do NOT echo the raw secret back" in rendered

    def test_old_permissive_clause_is_gone(self) -> None:
        """The v2 template used to authorize inline secrets via `parameters` — must be removed."""
        rendered = _render_agent_prompt()
        assert "redacted from the outbound client stream" not in rendered
        assert "you may pass it via `parameters`" not in rendered


class TestToolDocstringsRefusalClause:
    """Tool docstrings reach the agent via FunctionTool.description — they must agree with the prompt."""

    def _tools(self) -> list[object]:
        return [run_blocks_tool, update_and_run_blocks_tool]

    def test_old_permissive_clause_gone_from_tools(self) -> None:
        """The clause that told the agent inline secrets were fine via `parameters` is removed."""
        for tool in self._tools():
            desc = tool.description  # type: ignore[attr-defined]
            assert "redacted from" not in desc, f"{tool.name} still claims redaction"  # type: ignore[attr-defined]
            assert "you may pass it via" not in desc, f"{tool.name} still permits inline secrets"  # type: ignore[attr-defined]

    def test_new_refusal_reference_in_tools(self) -> None:
        """Docstrings point back at the CREDENTIAL HANDLING refusal rule in the system prompt."""
        import re

        cross_ref = re.compile(r"CREDENTIAL\s+HANDLING refusal rule")
        for tool in self._tools():
            desc = tool.description  # type: ignore[attr-defined]
            assert "do NOT pass" in desc, f"{tool.name} does not forbid inline secret pass-through"  # type: ignore[attr-defined]
            assert cross_ref.search(desc), f"{tool.name} does not cross-reference the refusal rule"  # type: ignore[attr-defined]

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
        assert "CREDENTIAL HANDLING refusal rule" in desc
