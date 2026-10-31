from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.forge.sdk.copilot import credential_pause as credential_pause_module
from skyvern.forge.sdk.copilot import request_policy as request_policy_module
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.agent import (
    RequestPolicyGuardrailInputs,
    _request_policy_agent_inputs,
    _rewrite_failed_test_response,
    _store_request_policy_on_context,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import (
    ApprovedCredential,
    StructuredContext,
    record_approved_credentials_in_global_llm_context,
)
from skyvern.forge.sdk.copilot.eval_llm_overrides import EvalLLMOverrides, eval_llm_override_scope
from skyvern.forge.sdk.copilot.request_policy import (
    RAW_SECRET_REFUSAL_SENTINEL,
    SAFETY_SCREEN_UNAVAILABLE_QUESTION,
    RequestPolicy,
    _seed_prior_approved_credentials,
    build_request_policy_trust_floor,
    credential_prompt_reason,
    screen_raw_secret_safety,
)
from skyvern.forge.sdk.copilot.tools.credential_fill import (
    _credential_fill_origin_grant,
    _credential_fill_prerequisite_error,
    _request_credential,
    _within_grant,
)
from skyvern.forge.sdk.copilot.tools.guardrails import _authority_tool_error, _update_and_run_requires_skipped_run
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotStreamMessageType
from tests.unit.copilot_test_helpers import make_copilot_ctx
from tests.unit.test_copilot_credential_pause import (
    _answered_cache,
    _make_credential,
    _stub_credential_lookup,
    _tool_ctx,
)

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
async def test_every_copy_of_a_cited_secret_is_redacted_including_curly_quoted_and_colon_prefixed() -> None:
    secret = "Zq8!vL3#pR2w"
    policy, _ = await _build(
        f"Use backup code {secret} first.\nIf asked again, type “{secret}” or enter PIN:{secret} and press Enter.",
        {"version": "1", "state": "detected", "citations": [secret]},
    )

    assert secret not in policy.canonical_user_message
    assert policy.canonical_user_message.count("[REDACTED_SECRET]") == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("secret", "citation"),
    [("Hunter2:Portal!", "Portal!"), ("Hunter2‘Portal!", "Portal!"), ("Hunter2’Portal!", "Hunter2")],
    ids=["after-colon", "after-curly-quote", "before-curly-quote"],
)
async def test_citing_part_of_a_secret_split_by_a_colon_or_curly_quote_fails_closed(secret: str, citation: str) -> None:
    policy, _ = await _build(
        f"My portal login is {secret}",
        {"version": "1", "state": "detected", "citations": [citation]},
    )

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "invalid_citation"
    assert "Hunter2" not in policy.canonical_user_message
    assert "Portal" not in policy.canonical_user_message


@pytest.mark.asyncio
async def test_a_cited_value_that_also_ends_a_longer_secret_leaves_no_part_of_it() -> None:
    policy, _ = await _build(
        "Use Portal! at the kiosk. The admin login is Hunter2:Portal! and the backup is “Portal!”.",
        {"version": "1", "state": "detected", "citations": ["Portal!"]},
    )

    assert policy.raw_secret_safety_status == "detected"
    assert "Portal!" not in policy.canonical_user_message
    assert "Hunter2" not in policy.canonical_user_message
    assert "Use [REDACTED_SECRET] at the kiosk." in policy.canonical_user_message


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
@pytest.mark.parametrize(
    "tool_name",
    [
        "run_blocks_and_collect_debug",
        "edit_block_and_run",
        "discover_workflow_entrypoint",
        "search_web",
        "run_browser_code",
        "inspect_page_for_composition",
        "inspect_locator_matches",
    ],
)
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
async def test_verified_cited_raw_secret_blocks_saved_credential_fill() -> None:
    literal = "Hunter2Portal!"
    policy, _ = await _build(
        f"The password is {literal}",
        {"version": "1", "state": "detected", "citations": [literal]},
    )
    ctx = make_copilot_ctx(request_policy=policy)
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER

    assert _credential_fill_prerequisite_error(ctx, "cred_1") is not None


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


_CARD_SECRET = "Sentinel-Pass-7Q2"
_CARD_LOGIN = "sentinel-login"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_url",
    [
        f"https://portal.example.com/login?user={_CARD_LOGIN}&password={_CARD_SECRET}",
        f"https://portal.example.com/{_CARD_LOGIN}/{_CARD_SECRET}/login",
    ],
    ids=["query", "path"],
)
async def test_redacted_secret_turn_opens_the_card_for_the_users_site_and_keeps_the_run_gate_closed(
    monkeypatch: pytest.MonkeyPatch, user_url: str
) -> None:
    message = (
        f"Sign in at {user_url} with username {_CARD_LOGIN} and password {_CARD_SECRET}, then download the invoice"
    )
    policy, _ = await _build(message, {"version": "1", "state": "detected", "citations": [_CARD_SECRET]})
    assert policy.raw_secret_redacted_draft
    ctx = _tool_ctx(monkeypatch, _answered_cache("connected", "cred_1"))
    monkeypatch.setattr(credential_pause_module, "_new_resume_token", lambda: "tok-1")
    ctx.request_policy = policy
    ctx.test_after_update_done = True
    _stub_credential_lookup(monkeypatch, _make_credential())

    with capture_logs() as logs:
        result = await _request_credential("https://portal.example.com/sign-in", "The portal needs a sign-in.", ctx)
        carried = record_approved_credentials_in_global_llm_context(ctx, None)

    assert result["status"] == "connected"
    ctx.last_workflow = object()
    ctx.last_update_block_count = 1
    final = _rewrite_failed_test_response(f"Done. Bound your saved login for {user_url}.", ctx)
    assert "untested" in final
    frame = ctx.stream.send.await_args_list[0].args[0]
    assert frame.type is WorkflowCopilotStreamMessageType.CREDENTIAL_REQUIRED
    assert frame.login_page_urls == ["https://portal.example.com"]
    surfaces = {
        "frame": frame.model_dump_json(),
        "result": json.dumps(result),
        "ask_urls": json.dumps(policy.credential_ask_login_page_urls),
        "admitted": json.dumps(policy.live_page_admitted_urls),
        "carried": carried or "",
        "final": final,
        "logs": json.dumps(logs, default=str),
    }
    for surface, text in surfaces.items():
        assert _CARD_SECRET not in text, surface
        assert _CARD_LOGIN not in text, surface

    assert policy.allow_run_blocks is False
    assert policy.allow_missing_credentials_in_draft is True
    assert ctx.test_after_update_done is True
    assert credential_prompt_reason(policy) is None
    assert _update_and_run_requires_skipped_run(ctx, "update_and_run_blocks") is True
    for tool_name in (
        "run_blocks_and_collect_debug",
        "search_web",
        "run_browser_code",
        "inspect_page_for_composition",
        "inspect_locator_matches",
    ):
        assert _authority_tool_error(ctx, tool_name) is not None
    grant, error = await _credential_fill_origin_grant(ctx, "cred_1")
    assert grant is None
    assert error is not None

    next_policy = RequestPolicy()
    await _seed_prior_approved_credentials(next_policy, organization_id="org-1", global_llm_context=carried)
    next_ctx = make_copilot_ctx(request_policy=next_policy)
    next_ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    next_grant, next_error = await _credential_fill_origin_grant(next_ctx, "cred_1")
    assert next_error is None
    assert next_grant is not None
    assert _within_grant("https://portal.example.com/password", next_grant)
    assert _authority_tool_error(next_ctx, "run_blocks_and_collect_debug") is None


@pytest.mark.asyncio
async def test_a_user_url_carrying_userinfo_authorizes_no_card_on_a_redacted_secret_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = f"Sign in at https://{_CARD_LOGIN}:{_CARD_SECRET}@portal.example.com/login and download the invoice"
    policy, _ = await _build(message, {"version": "1", "state": "detected", "citations": [_CARD_SECRET]})
    assert policy.raw_secret_redacted_draft
    ctx = _tool_ctx(monkeypatch, _answered_cache("connected", "cred_1"))
    ctx.request_policy = policy

    result = await _request_credential("https://portal.example.com/login", "The portal needs a sign-in.", ctx)

    assert result["ok"] is False
    ctx.stream.send.assert_not_awaited()
    assert policy.credential_ask_login_page_urls == []
    assert _CARD_SECRET not in json.dumps(result)


@pytest.mark.parametrize(
    ("user_url", "card_can_open", "expected"),
    [
        ("https://portal.example.com/login?pw=" + _CARD_SECRET, True, "request_credential"),
        ("", True, "ask the user for it"),
        ("https://portal.example.com/login", False, "Credentials UI"),
    ],
    ids=["user_site", "no_site", "card_unavailable"],
)
def test_a_redacted_secret_draft_result_points_at_the_card_while_it_can_open(
    monkeypatch: pytest.MonkeyPatch, user_url: str, card_can_open: bool, expected: str
) -> None:
    ctx = _tool_ctx(monkeypatch)
    ctx.client_supports_credential_pause = card_can_open
    ctx.request_policy = RequestPolicy(user_provided_site_urls=[user_url] if user_url else [])
    ctx.request_policy.apply_raw_secret_redacted_draft()
    result: dict[str, Any] = {"ok": True}

    tools_module._mark_credential_deferred_draft(ctx, result)

    message = result["data"]["message"]
    assert expected in message
    assert _CARD_SECRET not in message
    assert ("Credentials UI" in message) is not card_can_open


_CLEAN_VERDICT = {"version": "1", "state": "clean", "citations": []}
_SCREEN_PROMPT_TYPE = "workflow-copilot-raw-secret-safety"
_CLEAN_MESSAGE = "Build a workflow that downloads the invoice"


class _SafetyLeg:
    def __init__(
        self,
        llm_key: str,
        response: object = None,
        *,
        delay: float = 0.0,
        error: type[Exception] | None = None,
        hang: bool = False,
    ) -> None:
        self.llm_key = llm_key
        self.response = response
        self.delay = delay
        self.error = error
        self.hang = hang
        self.started_at: list[float] = []
        self.cancelled = False

    async def __call__(self, **_: object) -> object:
        self.started_at.append(time.monotonic())
        try:
            if self.hang:
                await asyncio.Event().wait()
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error("provider unavailable")
        return self.response


def _route_with_fallback(monkeypatch: pytest.MonkeyPatch, fallback: _SafetyLeg) -> list[str]:
    builds: list[str] = []

    def build(llm_key: str) -> _SafetyLeg:
        builds.append(llm_key)
        return fallback

    monkeypatch.setattr(request_policy_module, "router_fallback_llm_key", {"PRIMARY": "FALLBACK", "SELF": "SELF"}.get)
    monkeypatch.setattr(request_policy_module.LLMAPIHandlerFactory, "get_llm_api_handler", build)
    return builds


async def _trust_floor(message: str, handler: _SafetyLeg) -> RequestPolicy:
    return await build_request_policy_trust_floor(
        user_message=message,
        workflow_yaml="",
        chat_history=[],
        global_llm_context="",
        organization_id="org-1",
        handler=handler,
    )


@pytest.mark.asyncio
async def test_hung_primary_is_answered_by_the_fallback_inside_the_screen_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _SafetyLeg("PRIMARY", hang=True)
    fallback = _SafetyLeg("FALLBACK", _CLEAN_VERDICT, delay=0.5)
    builds = _route_with_fallback(monkeypatch, fallback)

    screen = await screen_raw_secret_safety(_CLEAN_MESSAGE, primary, organization_id="org-1")

    assert screen.status == "clean"
    assert screen.canonical_user_message == _CLEAN_MESSAGE
    assert (screen.answered_by, screen.hedged) == ("fallback", True)
    assert builds == ["FALLBACK"]
    assert fallback.started_at[0] - primary.started_at[0] >= settings.COPILOT_RAW_SECRET_SAFETY_HEDGE_DELAY_SECONDS
    assert screen.latency_ms < 4000
    assert primary.cancelled is True


@pytest.mark.asyncio
async def test_prompt_primary_never_builds_or_calls_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _SafetyLeg("PRIMARY", _CLEAN_VERDICT, delay=0.2)
    fallback = _SafetyLeg("FALLBACK", _CLEAN_VERDICT)
    builds = _route_with_fallback(monkeypatch, fallback)

    screen = await screen_raw_secret_safety(_CLEAN_MESSAGE, primary, organization_id="org-1")

    assert (screen.status, screen.answered_by, screen.hedged) == ("clean", "primary", False)
    assert builds == []
    assert fallback.started_at == []


@pytest.mark.asyncio
async def test_failed_primary_starts_the_fallback_without_waiting_for_the_hedge_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _SafetyLeg("PRIMARY", delay=0.1, error=RuntimeError)
    fallback = _SafetyLeg("FALLBACK", _CLEAN_VERDICT)
    _route_with_fallback(monkeypatch, fallback)

    screen = await screen_raw_secret_safety(_CLEAN_MESSAGE, primary, organization_id="org-1")

    assert (screen.status, screen.answered_by, screen.hedged) == ("clean", "fallback", True)
    assert fallback.started_at[0] - primary.started_at[0] < 1.0


@pytest.mark.asyncio
async def test_fallback_citation_redacts_the_cited_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "COPILOT_RAW_SECRET_SAFETY_HEDGE_DELAY_SECONDS", 0.1)
    primary = _SafetyLeg("PRIMARY", hang=True)
    fallback = _SafetyLeg("FALLBACK", {"version": "1", "state": "detected", "citations": ["Hunter2Portal!"]})
    _route_with_fallback(monkeypatch, fallback)

    screen = await screen_raw_secret_safety("The password is Hunter2Portal!", primary, organization_id="org-1")

    assert (screen.status, screen.handling, screen.answered_by) == ("detected", "redacted_draft", "fallback")
    assert screen.canonical_user_message == "The password is [REDACTED_SECRET]"


@pytest.mark.asyncio
async def test_invalid_fallback_verdict_is_final_and_withholds_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "COPILOT_RAW_SECRET_SAFETY_HEDGE_DELAY_SECONDS", 0.1)
    primary = _SafetyLeg("PRIMARY", _CLEAN_VERDICT, delay=0.5)
    fallback = _SafetyLeg("FALLBACK", {"version": "1", "state": "detected", "citations": ["not-in-turn-8391!"]})
    _route_with_fallback(monkeypatch, fallback)

    policy = await _trust_floor(_CLEAN_MESSAGE, primary)

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "invalid_citation"
    assert (policy.raw_secret_safety_answered_by, policy.raw_secret_safety_hedged) == ("fallback", True)
    assert policy.canonical_user_message == _SCREEN_UNAVAILABLE_TURN
    assert primary.cancelled is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fallback_response",
    [RuntimeError, {"version": "1", "state": "detected", "citations": ["Hunter2Portal!"]}],
)
async def test_primary_verdict_wins_a_same_tick_finish(
    monkeypatch: pytest.MonkeyPatch, fallback_response: object
) -> None:
    monkeypatch.setattr(settings, "COPILOT_RAW_SECRET_SAFETY_HEDGE_DELAY_SECONDS", 0.0)
    primary = _SafetyLeg("PRIMARY", _CLEAN_VERDICT)
    fallback = (
        _SafetyLeg("FALLBACK", error=RuntimeError)
        if fallback_response is RuntimeError
        else _SafetyLeg("FALLBACK", fallback_response)
    )
    _route_with_fallback(monkeypatch, fallback)

    screen = await screen_raw_secret_safety("The password is Hunter2Portal!", primary, organization_id="org-1")

    assert len(fallback.started_at) == 1
    assert (screen.status, screen.answered_by, screen.hedged) == ("clean", "primary", True)


@pytest.mark.asyncio
async def test_both_hung_legs_withhold_the_turn_at_the_screen_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "COPILOT_RAW_SECRET_SAFETY_HEDGE_DELAY_SECONDS", 0.1)
    monkeypatch.setattr(settings, "COPILOT_RAW_SECRET_SAFETY_TIMEOUT_SECONDS", 1.0)
    primary = _SafetyLeg("PRIMARY", hang=True)
    fallback = _SafetyLeg("FALLBACK", hang=True)
    _route_with_fallback(monkeypatch, fallback)

    policy = await _trust_floor("The password is Hunter2Portal!", primary)

    assert policy.raw_secret_safety_status == "blocked"
    assert policy.raw_secret_safety_failure_kind == "timeout"
    assert (policy.raw_secret_safety_answered_by, policy.raw_secret_safety_hedged) == ("none", True)
    assert policy.canonical_user_message == _SCREEN_UNAVAILABLE_TURN
    assert policy.allow_update_workflow is False
    assert policy.allow_run_blocks is False
    assert 1000 <= policy.raw_secret_safety_latency_ms < 1500
    assert (primary.cancelled, fallback.cancelled) == (True, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_hangs", [False, True])
@pytest.mark.parametrize(
    "route", ["hedge_disabled", "no_fallback_key", "self_fallback", "screen_eval_override", "fallback_build_error"]
)
async def test_screen_without_a_usable_fallback_makes_one_call(
    monkeypatch: pytest.MonkeyPatch, route: str, primary_hangs: bool
) -> None:
    monkeypatch.setattr(settings, "COPILOT_RAW_SECRET_SAFETY_HEDGE_ENABLED", route != "hedge_disabled")
    monkeypatch.setattr(settings, "COPILOT_RAW_SECRET_SAFETY_HEDGE_DELAY_SECONDS", 0.1)
    monkeypatch.setattr(settings, "COPILOT_RAW_SECRET_SAFETY_TIMEOUT_SECONDS", 0.5)
    llm_key = {"no_fallback_key": "SOLO", "self_fallback": "SELF"}.get(route, "PRIMARY")
    primary = _SafetyLeg(llm_key, hang=primary_hangs, error=RuntimeError)
    fallback = _SafetyLeg("FALLBACK", _CLEAN_VERDICT)
    builds = _route_with_fallback(monkeypatch, fallback)
    if route == "fallback_build_error":

        def build_fails(fallback_key: str) -> _SafetyLeg:
            builds.append(fallback_key)
            raise RuntimeError("deployment not configured")

        monkeypatch.setattr(request_policy_module.LLMAPIHandlerFactory, "get_llm_api_handler", build_fails)
    overrides = (
        EvalLLMOverrides("org-1", {_SCREEN_PROMPT_TYPE: llm_key}, {_SCREEN_PROMPT_TYPE: primary}, "eval-sha")
        if route == "screen_eval_override"
        else None
    )

    with eval_llm_override_scope(overrides, "wpid-1"):
        screen = await screen_raw_secret_safety(_CLEAN_MESSAGE, primary, organization_id="org-1")

    assert (screen.status, screen.failure_kind) == ("blocked", "timeout" if primary_hangs else "provider_error")
    assert (screen.answered_by, screen.hedged) == ("none", False)
    assert builds == (["FALLBACK"] if route == "fallback_build_error" else [])
    assert fallback.started_at == []
    assert len(primary.started_at) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["bound_method_handler", "acting_model_eval_override"])
async def test_hung_primary_still_hedges(monkeypatch: pytest.MonkeyPatch, variant: str) -> None:
    monkeypatch.setattr(settings, "COPILOT_RAW_SECRET_SAFETY_HEDGE_DELAY_SECONDS", 0.1)
    primary = _SafetyLeg("PRIMARY", hang=True)
    fallback = _SafetyLeg("FALLBACK", _CLEAN_VERDICT)
    _route_with_fallback(monkeypatch, fallback)
    # A bound method carries llm_key on __self__, not on the callable.
    handler = primary.__call__ if variant == "bound_method_handler" else primary
    overrides = (
        EvalLLMOverrides("org-1", {"workflow-copilot": "OTHER"}, {"workflow-copilot": _SafetyLeg("OTHER")}, "eval-sha")
        if variant == "acting_model_eval_override"
        else None
    )

    with eval_llm_override_scope(overrides, "wpid-1"):
        screen = await screen_raw_secret_safety(_CLEAN_MESSAGE, handler, organization_id="org-1")

    assert (screen.status, screen.answered_by, screen.hedged) == ("clean", "fallback", True)


@pytest.mark.asyncio
async def test_cancelling_the_screen_cancels_both_legs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "COPILOT_RAW_SECRET_SAFETY_HEDGE_DELAY_SECONDS", 0.1)
    primary = _SafetyLeg("PRIMARY", hang=True)
    fallback = _SafetyLeg("FALLBACK", hang=True)
    _route_with_fallback(monkeypatch, fallback)

    screen_task = asyncio.create_task(screen_raw_secret_safety(_CLEAN_MESSAGE, primary, organization_id="org-1"))
    await asyncio.sleep(0.3)
    assert fallback.started_at
    screen_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await screen_task
    assert (primary.cancelled, fallback.cancelled) == (True, True)
