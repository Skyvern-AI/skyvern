from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.copilot.agent import (
    RequestPolicyGuardrailInputs,
    _request_policy_agent_inputs,
    _store_request_policy_on_context,
)
from skyvern.forge.sdk.copilot.context import ApprovedCredential, StructuredContext
from skyvern.forge.sdk.copilot.request_policy import (
    RAW_SECRET_REFUSAL_SENTINEL,
    SAFETY_SCREEN_UNAVAILABLE_QUESTION,
    build_request_policy_trust_floor,
)
from skyvern.forge.sdk.copilot.tools.guardrails import _authority_tool_error, _update_and_run_requires_skipped_run
from tests.unit.copilot_test_helpers import make_copilot_ctx

_SCREEN_UNAVAILABLE_TURN = "[INPUT_UNAVAILABLE_SAFETY_SCREEN_INCOMPLETE]"
_ACCESS_LINK_VALUE = "A1B2C3D4E5F6"
_EXPLICIT_ACCESS_LINK_PROMPT = (
    "Make an agent that goes to "
    f"https://portal.example/?accesskey={_ACCESS_LINK_VALUE}#projects/123/dashboard\n\n"
    "and downloads all available files"
)
_EXPLICIT_TOKEN_LINK_PROMPT = (
    "Make an agent that downloads from https://portal.example/download?token=A1B2C3D4E5F6&signature=Z9Y8X7W6V5U4"
)
_STANDALONE_ACCESS_MATERIAL_PROMPT = (
    f"My access key is {_ACCESS_LINK_VALUE}. Use it as authentication material when drafting the workflow."
)


async def _build(
    message: str,
    response: object,
    org_credentials: list[SimpleNamespace] | None = None,
) -> tuple[object, AsyncMock]:
    handler = AsyncMock(return_value=response)
    with (
        patch(
            "skyvern.forge.app.DATABASE.credentials.get_credentials",
            new=AsyncMock(return_value=org_credentials or []),
        ),
        patch(
            "skyvern.forge.app.DATABASE.credentials.get_credentials_by_ids",
            new=AsyncMock(return_value=org_credentials or []),
        ),
    ):
        policy = await build_request_policy_trust_floor(
            user_message=message,
            workflow_yaml="",
            chat_history=[],
            global_llm_context="",
            organization_id="org-1",
            handler=handler,
        )
    return policy, handler


@pytest.mark.asyncio
async def test_carried_password_label_does_not_erase_prior_credential_approval() -> None:
    approved = SimpleNamespace(credential_id="cred_portal", name="portal-login")
    trusted_context = StructuredContext(
        approved_credentials=[ApprovedCredential(credential_id="cred_portal")],
        carried_trajectory=[{"tool_name": "fill", "selector": "#Password", "label": "Password:", "carried": True}],
    ).to_json_str()
    handler = AsyncMock(return_value={"version": "1", "state": "clean", "citations": []})
    with (
        patch(
            "skyvern.forge.app.DATABASE.credentials.get_credentials",
            new=AsyncMock(return_value=[approved]),
        ),
        patch(
            "skyvern.forge.app.DATABASE.credentials.get_credentials_by_ids",
            new=AsyncMock(return_value=[approved]),
        ),
    ):
        policy = await build_request_policy_trust_floor(
            user_message="run the workflow",
            workflow_yaml="",
            chat_history=[],
            global_llm_context=trusted_context,
            organization_id="org-1",
            handler=handler,
        )

    assert [credential.credential_id for credential in policy.resolved_credentials] == ["cred_portal"]


@pytest.mark.asyncio
async def test_semantic_secret_is_redacted_without_discarding_the_turn() -> None:
    literal = "Hunter2Portal!"
    policy, handler = await _build(
        f"Draft a login for the billing portal with {literal}",
        {"version": "1", "state": "detected", "citations": [literal]},
    )

    handler.assert_awaited_once()
    prompt = handler.await_args.kwargs["prompt"]
    assert literal in prompt
    assert policy.raw_secret_detected is True
    assert policy.raw_secret_handling == "redacted_draft"
    assert policy.raw_secret_safety_status == "detected"
    assert policy.raw_secret_safety_citation_count == 1
    assert policy.raw_secret_evidence is None
    # The secret is gone; everything the user asked for survives.
    assert literal not in policy.canonical_user_message
    assert policy.canonical_user_message == "Draft a login for the billing portal with [REDACTED_SECRET]"
    # Continues as an update-only draft rather than a refusal.
    assert policy.user_response_policy == "proceed"
    assert policy.allow_update_workflow is True
    assert policy.clarification_question is None
    # A turn that carried raw material still cannot drive a browser.
    assert policy.allow_run_blocks is False
    assert policy.allow_missing_credentials_in_draft is True


@pytest.mark.asyncio
async def test_explicit_access_link_destination_keeps_full_authoring_and_run_authority() -> None:
    policy, _ = await _build(
        _EXPLICIT_ACCESS_LINK_PROMPT,
        {"version": "1", "state": "clean", "citations": []},
    )

    agent_message, _ = _request_policy_agent_inputs(
        policy,
        user_message=_EXPLICIT_ACCESS_LINK_PROMPT,
        chat_history_text="",
        previous_user_message=None,
    )

    assert policy.raw_secret_safety_status == "clean"
    assert policy.raw_secret_safety_failure_kind == "none"
    assert policy.raw_secret_handling == "none"
    assert policy.canonical_user_message == _EXPLICIT_ACCESS_LINK_PROMPT
    assert policy.allow_update_workflow is True
    assert policy.allow_run_blocks is True
    assert policy.user_response_policy == "proceed"
    assert policy.clarification_question is None
    assert agent_message == _EXPLICIT_ACCESS_LINK_PROMPT


@pytest.mark.asyncio
async def test_explicit_destination_with_scoped_capability_parameters_reaches_model_unchanged() -> None:
    policy, handler = await _build(
        _EXPLICIT_TOKEN_LINK_PROMPT,
        {"version": "1", "state": "clean", "citations": []},
    )

    handler.assert_awaited_once()
    assert _EXPLICIT_TOKEN_LINK_PROMPT in handler.await_args.kwargs["prompt"]
    assert policy.raw_secret_safety_status == "clean"
    assert policy.canonical_user_message == _EXPLICIT_TOKEN_LINK_PROMPT
    assert policy.allow_update_workflow is True
    assert policy.allow_run_blocks is True


@pytest.mark.asyncio
async def test_secret_assignment_whose_value_is_a_url_keeps_deterministic_redaction() -> None:
    message = "api_key=https://portal.example/k/ABCDEF"
    policy, handler = await _build(
        message,
        {"version": "1", "state": "clean", "citations": []},
    )

    handler.assert_awaited_once()
    prompt = handler.await_args.kwargs["prompt"]
    assert message not in prompt
    assert "[REDACTED_SECRET]" in prompt
    assert policy.raw_secret_safety_status == "clean"
    assert policy.canonical_user_message == "[REDACTED_SECRET]"


@pytest.mark.asyncio
async def test_secret_assignment_starting_inside_url_and_ending_after_it_is_redacted() -> None:
    message = "Open https://portal.example/?api_key= ABCDEF"
    policy, handler = await _build(
        message,
        {"version": "1", "state": "clean", "citations": []},
    )

    handler.assert_awaited_once()
    prompt = handler.await_args.kwargs["prompt"]
    assert message not in prompt
    assert "[REDACTED_SECRET]" in prompt
    assert policy.canonical_user_message == "Open https://portal.example/?[REDACTED_SECRET]"


@pytest.mark.asyncio
async def test_account_row_pair_whose_value_is_a_url_keeps_deterministic_redaction() -> None:
    message = "Use user@example.test:www.portal.example"
    policy, handler = await _build(
        message,
        {"version": "1", "state": "clean", "citations": []},
    )

    handler.assert_awaited_once()
    prompt = handler.await_args.kwargs["prompt"]
    assert message not in prompt
    assert "[REDACTED_SECRET]" in prompt
    assert policy.canonical_user_message == "Use [REDACTED_SECRET]"


@pytest.mark.asyncio
async def test_url_auth_material_not_delegated_as_destination_remains_redacted() -> None:
    message = "Use the token from https://portal.example/account?token=A1B2C3D4E5F6 as authentication material."
    policy, handler = await _build(
        message,
        {"version": "1", "state": "detected", "citations": ["A1B2C3D4E5F6"]},
    )

    assert message in handler.await_args.kwargs["prompt"]
    assert policy.raw_secret_safety_status == "detected"
    assert policy.canonical_user_message == (
        "Use the token from https://portal.example/account?token=[REDACTED_SECRET] as authentication material."
    )
    assert policy.allow_update_workflow is True
    assert policy.allow_run_blocks is False


@pytest.mark.asyncio
async def test_reusable_password_inside_delegated_url_is_redacted_and_update_only() -> None:
    message = "Download from https://portal.example/export?password=Hunter2Portal!"
    policy, handler = await _build(
        message,
        {"version": "1", "state": "detected", "citations": ["Hunter2Portal!"]},
    )

    assert message in handler.await_args.kwargs["prompt"]
    assert policy.raw_secret_safety_status == "detected"
    assert policy.canonical_user_message == ("Download from https://portal.example/export?password=[REDACTED_SECRET]")
    assert policy.allow_update_workflow is True
    assert policy.allow_run_blocks is False


@pytest.mark.asyncio
async def test_reusable_password_in_url_userinfo_is_accepted_as_complete_citation() -> None:
    message = "Download from https://portal-user:Hunter2Portal!@portal.example/export"
    policy, _ = await _build(
        message,
        {"version": "1", "state": "detected", "citations": ["Hunter2Portal!"]},
    )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.raw_secret_safety_failure_kind == "none"
    assert policy.canonical_user_message == (
        "Download from https://portal-user:[REDACTED_SECRET]@portal.example/export"
    )


@pytest.mark.asyncio
async def test_partial_url_userinfo_password_citation_still_fails_closed() -> None:
    message = "Download from https://portal-user:Hunter2Portal!@portal.example/export"
    policy, _ = await _build(
        message,
        {"version": "1", "state": "detected", "citations": ["Portal!"]},
    )

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "invalid_citation"
    assert policy.canonical_user_message == _SCREEN_UNAVAILABLE_TURN


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["@", ":"])
async def test_userinfo_delimiters_do_not_validate_partial_query_value_citations(separator: str) -> None:
    message = f"Download from https://portal.example/export?password=Hunter2{separator}Pass"
    policy, _ = await _build(
        message,
        {"version": "1", "state": "detected", "citations": ["Hunter2"]},
    )

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "invalid_citation"
    assert policy.canonical_user_message == _SCREEN_UNAVAILABLE_TURN


@pytest.mark.asyncio
async def test_standalone_access_material_remains_redacted_and_update_only() -> None:
    policy, _ = await _build(
        _STANDALONE_ACCESS_MATERIAL_PROMPT,
        {"version": "1", "state": "detected", "citations": [_ACCESS_LINK_VALUE]},
    )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.raw_secret_safety_failure_kind == "none"
    assert policy.raw_secret_handling == "redacted_draft"
    assert policy.canonical_user_message == (
        "My access key is [REDACTED_SECRET]. Use it as authentication material when drafting the workflow."
    )
    assert policy.allow_update_workflow is True
    assert policy.allow_run_blocks is False


@pytest.mark.asyncio
async def test_multiple_semantic_secret_citations_are_each_redacted() -> None:
    first = "Hunter2Portal!"
    second = "BillingKey-8391!"
    policy, _ = await _build(
        f"Draft this config with {first} and {second}",
        {"version": "1", "state": "detected", "citations": [first, second]},
    )

    assert first not in policy.canonical_user_message
    assert second not in policy.canonical_user_message
    assert policy.canonical_user_message == "Draft this config with [REDACTED_SECRET] and [REDACTED_SECRET]"
    assert policy.raw_secret_handling == "redacted_draft"
    assert policy.raw_secret_safety_citation_count == 2


@pytest.mark.asyncio
async def test_citation_containing_another_citation_leaves_no_tail() -> None:
    outer = "Hunter2Portal-8391"
    inner = "8391"
    policy, _ = await _build(
        f"Draft with {outer} and {inner}",
        {"version": "1", "state": "detected", "citations": [inner, outer]},
    )

    assert outer not in policy.canonical_user_message
    assert policy.canonical_user_message == "Draft with [REDACTED_SECRET] and [REDACTED_SECRET]"


@pytest.mark.asyncio
async def test_partial_semantic_secret_citation_fails_closed() -> None:
    policy, _ = await _build(
        "The password is Hunter2Portal1234!",
        {"version": "1", "state": "detected", "citations": ["1234"]},
    )

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "invalid_citation"
    assert policy.canonical_user_message == _SCREEN_UNAVAILABLE_TURN


@pytest.mark.asyncio
async def test_complete_uri_query_value_citation_is_accepted_and_redacted() -> None:
    policy, _ = await _build(
        _EXPLICIT_ACCESS_LINK_PROMPT,
        {"version": "1", "state": "detected", "citations": [_ACCESS_LINK_VALUE]},
    )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.raw_secret_safety_failure_kind == "none"
    assert policy.canonical_user_message == (
        "Make an agent that goes to "
        "https://portal.example/?accesskey=[REDACTED_SECRET]#projects/123/dashboard\n\n"
        "and downloads all available files"
    )


@pytest.mark.asyncio
async def test_complete_uri_query_value_between_ampersands_is_accepted_and_redacted() -> None:
    message = f"Open https://portal.example/?view=all&accesskey={_ACCESS_LINK_VALUE}&project=123"
    policy, _ = await _build(
        message,
        {"version": "1", "state": "detected", "citations": [_ACCESS_LINK_VALUE]},
    )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.raw_secret_safety_failure_kind == "none"
    assert policy.canonical_user_message == (
        "Open https://portal.example/?view=all&accesskey=[REDACTED_SECRET]&project=123"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            f'Open "https://portal.example/?accesskey={_ACCESS_LINK_VALUE}"',
            'Open "https://portal.example/?accesskey=[REDACTED_SECRET]"',
        ),
        (
            f"Open https://portal.example/?accesskey={_ACCESS_LINK_VALUE}.",
            "Open https://portal.example/?accesskey=[REDACTED_SECRET].",
        ),
    ],
)
async def test_complete_uri_query_value_before_sentence_boundary_is_accepted(
    message: str,
    expected: str,
) -> None:
    policy, _ = await _build(
        message,
        {"version": "1", "state": "detected", "citations": [_ACCESS_LINK_VALUE]},
    )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.canonical_user_message == expected


@pytest.mark.asyncio
async def test_partial_uri_query_value_citation_still_fails_closed() -> None:
    policy, _ = await _build(
        _EXPLICIT_ACCESS_LINK_PROMPT,
        {"version": "1", "state": "detected", "citations": ["B2C3D4E5"]},
    )

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "invalid_citation"
    assert policy.canonical_user_message == _SCREEN_UNAVAILABLE_TURN


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["&", "#"])
async def test_uri_delimiter_does_not_validate_partial_citation_outside_url(separator: str) -> None:
    literal = f"A1B2C3D4{separator}E5F6"
    policy, _ = await _build(
        f"Use {literal} as authentication material.",
        {"version": "1", "state": "detected", "citations": ["A1B2C3D4"]},
    )

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "invalid_citation"
    assert policy.canonical_user_message == _SCREEN_UNAVAILABLE_TURN


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["&", "#"])
async def test_uri_delimiter_does_not_validate_partial_citation_after_delimiter_outside_url(separator: str) -> None:
    literal = f"Abc{separator}Def123"
    policy, _ = await _build(
        f"My password is {literal} ok",
        {"version": "1", "state": "detected", "citations": ["Def123"]},
    )

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "invalid_citation"
    assert policy.canonical_user_message == _SCREEN_UNAVAILABLE_TURN


@pytest.mark.asyncio
async def test_secret_ending_in_punctuation_can_precede_sentence_punctuation() -> None:
    literal = "BillingKey-8391!"
    policy, _ = await _build(
        f"Use {literal}.",
        {"version": "1", "state": "detected", "citations": [literal]},
    )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.raw_secret_safety_failure_kind == "none"
    assert policy.canonical_user_message == "Use [REDACTED_SECRET]."


@pytest.mark.asyncio
async def test_saved_credential_name_citation_is_redacted_not_exonerated() -> None:
    """A user-controlled credential name can itself equal a pasted secret."""
    message = 'Log into the analytics site with "analytics-portal-login" and export the dashboard'
    policy, _ = await _build(
        message,
        {"version": "1", "state": "detected", "citations": ["analytics-portal-login"]},
        org_credentials=[SimpleNamespace(credential_id="cred_1", name="analytics-portal-login")],
    )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.raw_secret_detected is True
    assert policy.raw_secret_safety_citation_count == 1
    assert policy.raw_secret_safety_exonerated_citation_count == 0
    assert policy.canonical_user_message == (
        'Log into the analytics site with "[REDACTED_SECRET]" and export the dashboard'
    )
    assert policy.user_response_policy == "proceed"
    assert policy.allow_update_workflow is True
    assert policy.allow_run_blocks is False


@pytest.mark.asyncio
async def test_saved_credential_exoneration_still_redacts_a_real_secret() -> None:
    literal = "Hunter2Portal!"
    credential_id = "cred_530111222333444555"
    policy, _ = await _build(
        f"Log in as {credential_id} with {literal}",
        {"version": "1", "state": "detected", "citations": [credential_id, literal]},
        org_credentials=[SimpleNamespace(credential_id=credential_id, name="analytics-portal-login")],
    )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.raw_secret_safety_citation_count == 1
    assert policy.raw_secret_safety_exonerated_citation_count == 1
    assert policy.canonical_user_message == f"Log in as {credential_id} with [REDACTED_SECRET]"


@pytest.mark.asyncio
async def test_credential_id_citation_is_exonerated() -> None:
    policy, _ = await _build(
        "Use cred_530111222333444555 for the login",
        {"version": "1", "state": "detected", "citations": ["cred_530111222333444555"]},
        org_credentials=[SimpleNamespace(credential_id="cred_530111222333444555", name="portal")],
    )

    assert policy.raw_secret_safety_status == "clean"
    assert policy.canonical_user_message == "Use cred_530111222333444555 for the login"


@pytest.mark.asyncio
async def test_org_visible_google_connection_id_citation_is_exonerated() -> None:
    connection_id = "goac_530111222333444555"
    with patch(
        "skyvern.forge.sdk.copilot.request_policy.google_oauth_service.get_visible_credentials_for_org",
        new=AsyncMock(return_value=[SimpleNamespace(id=connection_id)]),
    ):
        policy, _ = await _build(
            connection_id,
            {"version": "1", "state": "detected", "citations": [connection_id]},
        )

    assert policy.raw_secret_safety_status == "clean"
    assert policy.raw_secret_safety_exonerated_citation_count == 1
    assert policy.canonical_user_message == connection_id


@pytest.mark.asyncio
async def test_redaction_preserves_non_boundary_substring_occurrences() -> None:
    policy, _ = await _build(
        "Use pass as the password and open passport.example",
        {"version": "1", "state": "detected", "citations": ["pass"]},
    )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.canonical_user_message == "Use [REDACTED_SECRET] as the password and open passport.example"


@pytest.mark.asyncio
async def test_exoneration_lookup_failure_falls_back_to_redacting() -> None:
    credential_id = "cred_530111222333444555"
    handler = AsyncMock(return_value={"version": "1", "state": "detected", "citations": [credential_id]})
    with patch(
        "skyvern.forge.app.DATABASE.credentials.get_credentials_by_ids",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        policy = await build_request_policy_trust_floor(
            user_message=f"Log in with {credential_id}",
            workflow_yaml="",
            chat_history=[],
            global_llm_context="",
            organization_id="org-1",
            handler=handler,
        )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.canonical_user_message == "Log in with [REDACTED_SECRET]"


@pytest.mark.asyncio
async def test_non_secret_shaped_citation_is_redacted_not_rejected() -> None:
    """A word with no digit or punctuation used to fail the shape check and block the turn."""
    policy, _ = await _build(
        "Draft a login with passphrase alphabet",
        {"version": "1", "state": "detected", "citations": ["alphabet"]},
    )

    assert policy.raw_secret_safety_status == "detected"
    assert policy.raw_secret_safety_failure_kind == "none"
    assert policy.canonical_user_message == "Draft a login with passphrase [REDACTED_SECRET]"


@pytest.mark.asyncio
async def test_deterministic_and_semantic_redactions_merge_before_downstream_use() -> None:
    deterministic = "password=known-secret"
    semantic = "Hunter2Portal!"
    policy, handler = await _build(
        f"Draft with {deterministic} and {semantic}",
        {"version": "1", "state": "detected", "citations": [semantic]},
    )

    prompt = handler.await_args.kwargs["prompt"]
    assert deterministic not in prompt
    assert semantic in prompt
    assert deterministic not in policy.canonical_user_message
    assert semantic not in policy.canonical_user_message
    assert policy.canonical_user_message == "Draft with [REDACTED_SECRET] and [REDACTED_SECRET]"


@pytest.mark.asyncio
async def test_clean_verdict_keeps_deterministic_redaction_without_withdrawing_authority() -> None:
    policy, _ = await _build(
        'Use the saved credential mock-portal-login-totp; selector token: #token or input[name="otp"].',
        {"version": "1", "state": "clean", "citations": []},
    )

    assert "[REDACTED_SECRET]" in policy.canonical_user_message
    assert policy.raw_secret_detected is False
    assert policy.raw_secret_handling == "none"
    assert policy.raw_secret_safety_status == "clean"
    assert policy.raw_secret_safety_citation_count == 0
    assert policy.allow_run_blocks is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "failure"),
    [
        (
            {"version": "1", "state": "clean", "citations": ["Hunter2Portal!"]},
            "contradictory_verdict",
        ),
        (
            {"version": "1", "state": "detected", "citations": ["not-in-turn-8391!"]},
            "invalid_citation",
        ),
        ({"version": "1", "state": "detected", "citations": []}, "contradictory_verdict"),
        ({"state": "detected", "citations": ["Hunter2Portal!"]}, "malformed_output"),
        ("not-json", "malformed_output"),
    ],
)
async def test_invalid_safety_states_block_the_turn(response: object, failure: str) -> None:
    policy, _ = await _build("The password is Hunter2Portal!", response)

    assert policy.user_response_policy == "ask_clarification"
    assert policy.allow_update_workflow is False
    assert policy.allow_run_blocks is False
    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == failure


@pytest.mark.asyncio
async def test_unknown_verdict_keys_do_not_cost_the_turn() -> None:
    policy, _ = await _build(
        "Build a workflow that downloads the invoice",
        {"version": "1", "state": "clean", "handling": "none", "citations": [], "confidence": 0.9},
    )

    assert policy.raw_secret_safety_status == "clean"
    assert policy.raw_secret_safety_failure_kind == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_kind",
    ["invalid_citation", "malformed_output"],
)
async def test_screening_failure_is_not_reported_as_a_credential_paste(failure_kind: str) -> None:
    """An unavailable screen never accuses the user of pasting a secret."""
    response: object = (
        {"version": "1", "state": "detected", "citations": ["not-in-turn-8391!"]}
        if failure_kind == "invalid_citation"
        else "not-json"
    )
    policy, _ = await _build("Build a workflow that downloads the invoice", response)

    assert policy.raw_secret_safety_failure_kind == failure_kind
    assert policy.clarification_reason == "safety_screen_unavailable"
    assert policy.clarification_question == SAFETY_SCREEN_UNAVAILABLE_QUESTION
    assert RAW_SECRET_REFUSAL_SENTINEL not in (policy.clarification_question or "")
    assert "/credentials" not in (policy.clarification_question or "")
    assert policy.canonical_user_message == _SCREEN_UNAVAILABLE_TURN


@pytest.mark.asyncio
async def test_missing_dedicated_handler_blocks() -> None:
    policy = await build_request_policy_trust_floor(
        user_message="Hello",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        handler=None,
    )

    assert policy.user_response_policy == "ask_clarification"
    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "missing_handler"
    assert policy.clarification_reason == "safety_screen_unavailable"


@pytest.mark.asyncio
async def test_safety_timeout_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never_returns(**_: object) -> object:
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(settings, "COPILOT_RAW_SECRET_SAFETY_TIMEOUT_SECONDS", 0.001)
    policy = await build_request_policy_trust_floor(
        user_message="Hello",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        handler=_never_returns,
    )

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "timeout"


@pytest.mark.asyncio
async def test_safety_provider_failure_blocks() -> None:
    handler = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    policy = await build_request_policy_trust_floor(
        user_message="Hello",
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        handler=handler,
    )

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "provider_error"
    assert policy.allow_update_workflow is False
    assert policy.allow_run_blocks is False


@pytest.mark.asyncio
async def test_canonical_safe_turn_is_the_only_agent_input() -> None:
    literal = "Hunter2Portal!"
    policy, _ = await _build(
        f"Draft with {literal}",
        {"version": "1", "state": "detected", "citations": [literal]},
    )

    agent_message, _ = _request_policy_agent_inputs(
        policy,
        user_message=f"Draft with {literal}",
        chat_history_text="",
        previous_user_message=None,
    )

    assert agent_message == policy.canonical_user_message
    assert literal not in agent_message


_RAW_SECRET_MESSAGE = "Log into the portal with api_key='sk-abcdefghijklmnopqrstuvwxyz1234567890' and get the invoice"


async def _uncited_redaction_ctx(message: str = _RAW_SECRET_MESSAGE):
    """A deterministic redaction whose semantic safety verdict cited no secret."""
    policy, _ = await _build(message, {"version": "1", "state": "clean", "citations": []})
    ctx = make_copilot_ctx()
    _store_request_policy_on_context(
        ctx,
        policy,
        RequestPolicyGuardrailInputs(
            user_message=message,
            workflow_yaml="",
            chat_history_text="",
            chat_history_messages=[],
            global_llm_context="",
            organization_id="org-1",
            request_policy_handler=None,
        ),
        reconcile_completion_criteria=False,
    )
    return ctx, policy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["run_blocks_and_collect_debug", "discover_workflow_entrypoint", "inspect_page_for_composition", "search_web"],
)
async def test_uncited_deterministic_redaction_does_not_block_browser_tools(tool_name: str) -> None:
    ctx, policy = await _uncited_redaction_ctx()
    assert policy.raw_secret_detected is False
    assert policy.raw_secret_handling == "none"

    error = _authority_tool_error(ctx, tool_name)

    assert error is None
    assert ctx.blocker_signal is None


@pytest.mark.asyncio
async def test_uncited_deterministic_redaction_preserves_run_authority() -> None:
    ctx, policy = await _uncited_redaction_ctx()

    assert policy.allow_run_blocks is True
    assert _update_and_run_requires_skipped_run(ctx, "update_and_run_blocks") is False
    assert _authority_tool_error(ctx, "update_workflow") is None
    assert "sk-abcdefghijklmnopqrstuvwxyz1234567890" not in ctx.user_message


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["run_blocks_and_collect_debug", "discover_workflow_entrypoint", "search_web"])
async def test_verified_cited_raw_secret_blocks_browser_acting_tools(tool_name: str) -> None:
    literal = "Hunter2Portal!"
    policy, _ = await _build(
        f"The password is {literal}",
        {"version": "1", "state": "detected", "citations": [literal]},
    )
    ctx = make_copilot_ctx(request_policy=policy)

    error = _authority_tool_error(ctx, tool_name)

    assert error is not None
    assert ctx.blocker_signal is not None
    assert ctx.blocker_signal.internal_reason_code == "raw_secret_browser_action_blocked"
    assert ctx.blocker_signal.blocked_tool == tool_name


@pytest.mark.asyncio
async def test_verified_cited_raw_secret_does_not_block_read_only_page_inspection() -> None:
    literal = "Hunter2Portal!"
    policy, _ = await _build(
        f"The password is {literal}",
        {"version": "1", "state": "detected", "citations": [literal]},
    )
    ctx = make_copilot_ctx(request_policy=policy)

    assert _authority_tool_error(ctx, "inspect_page_for_composition") is None
    assert ctx.blocker_signal is None


@pytest.mark.asyncio
async def test_detected_turn_becomes_an_update_only_draft_carrying_the_users_intent() -> None:
    literal = "Hunter2Portal!"
    message = f"Build a workflow that logs into the billing portal with {literal} and downloads the invoice"
    policy, _ = await _build(message, {"version": "1", "state": "detected", "citations": [literal]})
    ctx = make_copilot_ctx()
    _store_request_policy_on_context(
        ctx,
        policy,
        RequestPolicyGuardrailInputs(
            user_message=message,
            workflow_yaml="",
            chat_history_text="",
            chat_history_messages=[],
            global_llm_context="",
            organization_id="org-1",
            request_policy_handler=None,
        ),
        reconcile_completion_criteria=False,
    )

    assert ctx.allow_untested_workflow_draft is True
    assert policy.testing_intent == "skip_test"
    assert policy.allow_run_blocks is False
    assert literal not in ctx.user_message
    assert "downloads the invoice" in ctx.user_message


@pytest.mark.asyncio
async def test_clean_turn_reaches_the_browser_and_runs() -> None:
    policy, _ = await _build(
        "Build a workflow that downloads the invoice",
        {"version": "1", "state": "clean", "citations": []},
    )
    ctx = make_copilot_ctx(request_policy=policy)

    assert policy.raw_secret_detected is False
    assert _authority_tool_error(ctx, "run_blocks_and_collect_debug") is None
    assert _update_and_run_requires_skipped_run(ctx, "update_and_run_blocks") is False
