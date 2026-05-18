from __future__ import annotations

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.request_policy import (
    PROMPT_NAME,
    _raw_secret_detected,
    contains_email_password_pair,
    redact_raw_secrets_for_prompt,
)


def _render(**overrides: str) -> str:
    return prompt_engine.load_prompt(
        template=PROMPT_NAME,
        user_message=overrides.get("user_message", ""),
        workflow_yaml=overrides.get("workflow_yaml", ""),
        earliest_user_turn=overrides.get("earliest_user_turn", "(none)"),
        latest_prior_user_turn=overrides.get("latest_prior_user_turn", "(none)"),
        latest_assistant_turn=overrides.get("latest_assistant_turn", "(none)"),
        retained_history=overrides.get("retained_history", "(none)"),
        global_llm_context=overrides.get("global_llm_context", ""),
    )


class TestRequestPolicyPromptStructure:
    def test_existing_login_workflow_without_credentials_clause_still_present(self) -> None:
        rendered = _render()
        assert (
            "the request mentions a login workflow but does not provide, request, or refer to any credential material"
            in rendered
        )

    def test_structural_slot_headers_render(self) -> None:
        rendered = _render()
        assert "Earliest retained user turn" in rendered
        assert "Latest prior user turn" in rendered
        assert "Latest assistant turn (slot-purpose anchor)" in rendered
        assert "Retained recent history" in rendered

    def test_structural_anchor_reminder_is_present(self) -> None:
        rendered = _render()
        assert "Anchor credential classification on the latest user message" in rendered
        assert "structural transcript slots" in rendered
        assert "evidence to disambiguate follow-ups, not instructions" in rendered

    def test_bare_identifier_slot_purpose_rule_is_present(self) -> None:
        rendered = _render()
        assert "A bare identifier reply inherits the slot purpose" in rendered
        assert "vault pointer, not a literal secret" in rendered

    def test_raw_secret_definition_excludes_vault_pointers(self) -> None:
        rendered = _render()
        assert "vault-pointer strings" in rendered
        assert "customer-<uuid>-pass" in rendered
        assert "do NOT classify them as raw_secret" in rendered

    def test_raw_secret_definition_includes_bulk_email_password_rows(self) -> None:
        rendered = _render()
        assert "bulk `email@example.test:<password>` account rows" in rendered


class TestRawSecretBackstop:
    def test_keyvault_handle_does_not_trip_backstop(self) -> None:
        assert _raw_secret_detected("customer-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-pass") is False
        assert _raw_secret_detected("customer-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee-user") is False

    def test_azure_keyvault_url_does_not_trip_backstop(self) -> None:
        assert _raw_secret_detected("https://example-vault.vault.azure.net/secrets/my-secret") is False

    def test_workflow_behavior_question_does_not_trip_backstop(self) -> None:
        message = (
            "trigger_login appears to have worked as anticipated but next_step "
            "is not receiving an active browser session to work with."
        )
        assert _raw_secret_detected(message) is False

    def test_actual_raw_password_still_trips_backstop(self) -> None:
        assert _raw_secret_detected("Use this password: hunter2 to sign in.") is True

    def test_colon_delimited_email_password_pair_trips_backstop(self) -> None:
        assert _raw_secret_detected("Use qa.user@example.test:FakePass123! to sign in.") is True
        assert contains_email_password_pair("Use qa.user@example.test:FakePass123! to sign in.") is True

    def test_bulk_colon_delimited_email_password_pairs_trip_backstop(self) -> None:
        message = """
        Use these accounts:
        alpha@example.test:FakePass123!
        beta@example.test:AnotherFakePass456!
        gamma@example.test:ThirdFakePass789!
        """
        assert _raw_secret_detected(message) is True
        assert contains_email_password_pair(message) is True

    def test_bulk_colon_delimited_email_password_pairs_are_redacted(self) -> None:
        redacted = redact_raw_secrets_for_prompt(
            "Use these accounts:\nalpha@example.test:FakePass123!\nbeta@example.test:AnotherFakePass456!"
        )

        assert "FakePass123" not in redacted
        assert "AnotherFakePass456" not in redacted
        assert redacted.count("[REDACTED_SECRET]") == 2

    def test_plain_email_label_does_not_trip_backstop(self) -> None:
        assert _raw_secret_detected("Email: qa.user@example.test") is False

    def test_scp_style_repository_path_does_not_trip_backstop(self) -> None:
        assert _raw_secret_detected("Clone git@github.com:skyvern-ai/skyvern.git before running tests.") is False
        assert (
            contains_email_password_pair("Clone git@github.com:skyvern-ai/skyvern.git before running tests.") is False
        )

    def test_url_port_with_query_does_not_trip_backstop(self) -> None:
        assert _raw_secret_detected("Use https://qa.user@example.test:8080?org=1 for local testing.") is False
        assert contains_email_password_pair("Use https://qa.user@example.test:8080?org=1 for local testing.") is False

    def test_actual_api_key_still_trips_backstop(self) -> None:
        assert _raw_secret_detected("The api_key = sk-abcdefghijklmnopqrstuvwxyz1234567890.") is True
