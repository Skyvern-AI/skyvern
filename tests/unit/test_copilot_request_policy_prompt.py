from __future__ import annotations

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.request_policy import (
    PROMPT_NAME,
    _clip_chat_history_for_prompt,
    _raw_secret_detected,
)


def _render(**overrides: str) -> str:
    return prompt_engine.load_prompt(
        template=PROMPT_NAME,
        user_message=overrides.get("user_message", ""),
        workflow_yaml=overrides.get("workflow_yaml", ""),
        chat_history=overrides.get("chat_history", ""),
        global_llm_context=overrides.get("global_llm_context", ""),
    )


class TestRequestPolicyPromptRules:
    def test_existing_carry_forward_rule_still_present(self) -> None:
        rendered = _render()
        assert "Use recent chat history when classifying a follow-up" in rendered
        assert "carry those names forward in credential_refs" in rendered

    def test_existing_login_workflow_without_credentials_clause_still_present(self) -> None:
        rendered = _render()
        assert (
            "the request mentions a login workflow but does not provide, request, or refer to any credential material"
            in rendered
        )

    def test_credential_request_anchor_rule(self) -> None:
        rendered = _render()
        assert (
            "Credential classification anchors on whether the latest user message itself requests credentials"
            in rendered
        )
        assert "topic mentions, not credential requests" in rendered
        assert "regardless of how much credential context lives in workflow_yaml or chat_history" in rendered

    def test_credentials_jinja_token_renders_literally(self) -> None:
        rendered = _render()
        assert "`{{ credentials }}`" in rendered

    def test_identifier_shaped_bare_reply_rule(self) -> None:
        rendered = _render()
        assert "Identifier-shaped bare replies" in rendered
        assert "inherit the most recent assistant turn's slot purpose" in rendered
        assert "key-vault handles are vault pointers, not Skyvern saved-credential names" in rendered

    def test_bare_reply_rule_preserves_raw_secret_classification(self) -> None:
        rendered = _render()
        assert "does NOT relax raw_secret detection" in rendered
        assert "remains credential_input_kind=raw_secret regardless" in rendered


class TestRawSecretRegexBackstop:
    def test_keyvault_handle_does_not_trip_regex(self) -> None:
        assert _raw_secret_detected("customer-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-pass") is False
        assert _raw_secret_detected("customer-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-user") is False

    def test_azure_keyvault_url_does_not_trip_regex(self) -> None:
        assert _raw_secret_detected("https://example-vault.vault.azure.net/secrets/my-secret") is False

    def test_workflow_behavior_question_does_not_trip_regex(self) -> None:
        message = (
            "trigger_login appears to have worked as anticipated but next_step "
            "is not receiving an active browser session to work with."
        )
        assert _raw_secret_detected(message) is False

    def test_actual_raw_password_still_trips_regex(self) -> None:
        assert _raw_secret_detected("Use this password: hunter2 to sign in.") is True

    def test_actual_api_key_still_trips_regex(self) -> None:
        assert _raw_secret_detected("The api_key = sk-abcdefghijklmnopqrstuvwxyz1234567890.") is True


class TestClipChatHistoryForPrompt:
    def test_short_history_passes_through_unchanged(self) -> None:
        text = "user: hi\nassistant: hello"
        assert _clip_chat_history_for_prompt(text, limit=2048) == text

    def test_long_history_preserves_latest_assistant_turn(self) -> None:
        filler = "user: pad pad pad pad pad pad pad pad pad pad\n" * 200
        latest = "assistant: What value should I use for password_key_vault_id?"
        text = filler + latest
        clipped = _clip_chat_history_for_prompt(text, limit=2048, head=384)
        assert latest in clipped, "latest assistant turn must survive middle-truncation"
        assert len(clipped) <= 2048
        assert "[earlier chat history truncated]" in clipped

    def test_long_history_preserves_first_user_turn(self) -> None:
        first = "user: build a workflow that consolidates these blocks\n"
        filler = "user: ack\nassistant: ack\n" * 200
        text = first + filler
        clipped = _clip_chat_history_for_prompt(text, limit=2048, head=384)
        assert first.rstrip() in clipped, "first user turn must survive middle-truncation"

    def test_tiny_limit_falls_back_to_tail(self) -> None:
        text = "abcdefghij" * 50
        clipped = _clip_chat_history_for_prompt(text, limit=20, head=384)
        assert len(clipped) == 20
        assert clipped == text[-20:]

    def test_head_boundary_snaps_to_last_newline(self) -> None:
        first = "user: this is a longer opening turn with multiple words to push past 200 chars " * 4
        latest = "\nassistant: What value should I use for password_key_vault_id?"
        text = first + ("\nuser: filler\nassistant: filler\n" * 200) + latest
        clipped = _clip_chat_history_for_prompt(text, limit=2048, head=384)
        head_block = clipped.split("...[earlier chat history truncated]...")[0]
        assert head_block.endswith("\n"), "head should snap to a newline boundary, not mid-turn"
