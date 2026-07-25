"""`_make_agent_result` back-fills the typed terminal adjudication onto the
narrative payload: ``responseKind`` from ``TurnOutcome.response_kind`` and
``verifiedSuccess`` from ``enforcement.verified_goal_satisfied_context``."""

from __future__ import annotations

import base64
import json
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image, PngImagePlugin

from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.agent import _finalize_result_with_blocker_override, _make_agent_result
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.context import (
    AgentResult,
    CopilotContext,
    DeliveredUnverifiedPublicOutputs,
    StructuredContext,
)
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from tests.unit.copilot_test_helpers import make_copilot_ctx as _ctx
from tests.unit.copilot_test_helpers import make_verified_goal_contract


def _verified_goal_ctx() -> CopilotContext:
    return _ctx(
        last_test_ok=True,
        last_full_workflow_test_ok=True,
        latest_diagnosis_repair_contract=make_verified_goal_contract(),
    )


def _outcome(kind: ResponseKind) -> TurnOutcome:
    return TurnOutcome(response_kind=kind)


def _payload(**overrides: object) -> dict:
    base: dict = {
        "turnId": "turn-1",
        "turnIndex": 0,
        "mode": "build",
        "designStarted": True,
        "designEnded": True,
        "draft": None,
        "blocks": [],
        "terminal": "response",
        "terminalMessage": "done",
        "narrativeSummary": None,
        "priorBlockCount": None,
        "designActivity": [],
        "startedAt": None,
        "endedAt": None,
    }
    base.update(overrides)
    return base


def _result(ctx: CopilotContext | None, **kwargs: object):
    if (
        ctx is not None
        and ctx.delivered_unverified_terminal
        and ctx.delivered_unverified_observed_outputs
        and not isinstance(ctx.delivered_unverified_observed_outputs, DeliveredUnverifiedPublicOutputs)
    ):
        ctx.delivered_unverified_observed_outputs = DeliveredUnverifiedPublicOutputs(
            ctx.delivered_unverified_observed_outputs
        )
    kwargs.setdefault("user_response", "ok")
    kwargs.setdefault("updated_workflow", None)
    kwargs.setdefault("global_llm_context", None)
    if ctx is not None and ctx.delivered_unverified_terminal:
        kwargs.setdefault("_delivered_unverified_snapshot", agent_module._delivered_unverified_observed_outputs(ctx))
    return _make_agent_result(ctx, **kwargs)


def test_backfill_writes_both_fields_together() -> None:
    result = _result(_ctx(), turn_outcome=_outcome(ResponseKind.CLARIFY), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "clarify"
    assert result.narrative_payload["verifiedSuccess"] is False


def test_backfill_verified_success_requires_adjudicated_evidence() -> None:
    # The legacy run-status conjunction still ends the turn but no longer backs
    # a verified-success claim: without judge-confirmed outcome evidence the
    # claim tier renders built-but-unverified.
    result = _result(_verified_goal_ctx(), turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "build"
    assert result.narrative_payload["verifiedSuccess"] is False


def test_backfill_verified_success_true_when_outcome_fully_verified() -> None:
    from skyvern.forge.sdk.copilot.completion_verification import (
        CompletionVerificationResult,
        CriterionVerdict,
    )

    ctx = _verified_goal_ctx()
    ctx.completion_verification_result = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=["c0"],
        verdicts=[CriterionVerdict(criterion_id="c0", state="satisfied", reason_code="evidence_confirms")],
    )
    result = _result(ctx, turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["verifiedSuccess"] is True


def test_backfill_never_overwrites_explicit_values() -> None:
    payload = _payload(responseKind="refuse", verifiedSuccess=True)
    result = _result(_ctx(), turn_outcome=_outcome(ResponseKind.CLARIFY), narrative_payload=payload)
    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "refuse"
    assert result.narrative_payload["verifiedSuccess"] is True


def test_backfill_tolerates_turn_outcome_none() -> None:
    result = _result(_ctx(), turn_outcome=None, narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert "responseKind" not in result.narrative_payload
    assert result.narrative_payload["verifiedSuccess"] is False


def test_backfill_tolerates_ctx_none() -> None:
    result = _result(None, turn_outcome=_outcome(ResponseKind.REFUSE), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["responseKind"] == "refuse"
    assert "verifiedSuccess" not in result.narrative_payload


def test_backfill_tolerates_missing_payload() -> None:
    with pytest.raises(ValueError, match="narrative_payload"):
        _result(_ctx(), turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=None)


def test_missing_payload_is_allowed_without_ctx() -> None:
    result = _result(None, turn_outcome=_outcome(ResponseKind.BUILD), narrative_payload=None)
    assert result.narrative_payload is None


def test_backfill_adds_credential_prompt_for_typed_clarification_reason() -> None:
    ctx = _ctx(request_policy=RequestPolicy(clarification_reason="credential_name_unresolved"))
    result = _result(ctx, turn_outcome=_outcome(ResponseKind.CLARIFY), narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert result.narrative_payload["credentialPrompt"] == {"reason": "credential_name_unresolved"}


def test_blocker_override_path_adds_credential_prompt_from_request_policy() -> None:
    ctx = _ctx(request_policy=RequestPolicy(clarification_reason="workflow_credential_inputs_unbound"))
    ctx.blocker_signal = CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="Reply to the user without updating the workflow.",
        user_facing_reason="I couldn't find the required credentials for the existing workflow.",
        recovery_hint="report_blocker_to_user",
        internal_reason_code="turn_intent_no_mutation_run_blocked",
        blocked_tool="update_workflow",
    )
    pre_override = AgentResult(user_response="agent reply", updated_workflow=None, global_llm_context=None)

    overridden = _finalize_result_with_blocker_override(ctx, pre_override)

    assert overridden.narrative_payload is not None
    assert overridden.narrative_payload["credentialPrompt"] == {"reason": "workflow_credential_inputs_unbound"}


def test_backfill_adds_credential_prompt_from_text_marker_when_no_policy_signal() -> None:
    result = _result(
        _ctx(),
        user_response="You can add one at https://app.skyvern.com/credentials.",
        narrative_payload=_payload(),
    )
    assert result.narrative_payload is not None
    assert result.narrative_payload["credentialPrompt"] == {"reason": "assistant_directed"}


def test_backfill_omits_credential_prompt_when_no_signal_present() -> None:
    result = _result(_ctx(), user_response="Done, the workflow is ready.", narrative_payload=_payload())
    assert result.narrative_payload is not None
    assert "credentialPrompt" not in result.narrative_payload


def test_backfill_adds_sanitized_delivered_unverified_outputs_with_json_types() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_workflow_run_id = "wr_source_123"
    ctx.secret_scrub_values.append("registered-secret-value")
    deeply_nested: dict[str, Any] = {"password": "deep-must-not-persist"}
    for index in range(24):
        deeply_nested = {f"level_{index}": deeply_nested}
    ctx.delivered_unverified_observed_outputs = {
        "result": {
            "account": "acct-123",
            "amount": 0,
            "confirmed": False,
            "items": ["first", None, 2.5],
            "password": "must-not-persist",
            "registered": "prefix registered-secret-value suffix",
            "raw": "api_key=sk-1234567890abcdefghijkl",
            "internal": "run wr_internal_123 with update_workflow",
            "deep": deeply_nested,
            "api_key=sk-raw-secret-key-1234567890": "safe-value",
            7: "non-string-key-value",
        }
    }

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["$skyvernOutput"] == {"omitted": {"depth": 1, "unsupported": 1}}
    assert "wr_source_123" not in str(observed)
    assert observed["result"]["account"] == "acct-123"
    assert observed["result"]["amount"] == 0
    assert observed["result"]["confirmed"] is False
    assert observed["result"]["items"] == ["first", None, 2.5]
    assert observed["result"]["password"] == "****"
    assert observed["result"]["registered"] == "prefix [REDACTED_SECRET] suffix"
    assert observed["result"]["raw"] == "[REDACTED_SECRET]"
    assert "deep-must-not-persist" not in str(observed)
    assert "sk-raw-secret-key-1234567890" not in str(observed)
    assert "non-string-key-value" not in str(observed)
    assert "wr_internal_123" not in str(observed)
    assert "update_workflow" not in str(observed)


def test_backfill_rejects_non_finite_numbers_and_remains_strict_json() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {
        "result": {
            "finite": 1.25,
            "not_a_number": float("nan"),
            "positive_infinity": float("inf"),
            "negative_infinity": float("-inf"),
            "items": [0.0, float("nan"), 2.5],
        }
    }

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    omission = {"$skyvernOmitted": {"reason": "non-finite number", "count": 1}}
    assert observed["result"] == {
        "finite": 1.25,
        "not_a_number": omission,
        "positive_infinity": omission,
        "negative_infinity": omission,
        "items": [0.0, omission, 2.5],
    }
    assert json.loads(json.dumps(observed, allow_nan=False)) == observed


def test_delivered_unverified_backfill_preserves_valid_image_when_registered_secret_is_unassociated() -> None:
    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAIAAAACUFjqAAAAE0lEQVR4nGP8z4APMOGVZRip0gBBLAETee26JgAAAABJRU5ErkJggg=="
    embedded = png_b64[20:30]
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.secret_scrub_values.append(embedded)
    ctx.delivered_unverified_observed_outputs = {
        "result": {
            "screenshot_base64": png_b64,
            "note": f"captured value {embedded}",
        }
    }

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    canonical = base64.b64decode(observed["result"]["screenshot_base64"], validate=True)
    with Image.open(BytesIO(canonical)) as image:
        image.load()
        assert image.format == "PNG"
        assert image.info == {}
    assert embedded not in observed["result"]["note"]


def test_make_agent_result_reencodes_png_without_secret_bearing_text_metadata() -> None:
    secret = "registered-secret-value"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("comment", secret)
    source = BytesIO()
    Image.new("RGB", (1, 1), (12, 34, 56)).save(source, format="PNG", pnginfo=metadata)
    source_b64 = base64.b64encode(source.getvalue()).decode()
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.secret_scrub_values.append(secret)
    ctx.delivered_unverified_observed_outputs = {"result": {"screenshot_base64": source_b64}}

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    published_b64 = observed["result"]["screenshot_base64"]
    published = base64.b64decode(published_b64, validate=True)
    assert published_b64 != source_b64
    assert secret.encode() not in published
    with Image.open(BytesIO(published)) as image:
        image.load()
        assert image.format == "PNG"
        assert image.info == {}


def test_make_agent_result_reencodes_jpeg_without_secret_bearing_exif_metadata() -> None:
    secret = "registered-secret-value"
    exif = Image.Exif()
    exif[0x010E] = secret
    source = BytesIO()
    Image.new("RGB", (2, 2), (12, 34, 56)).save(source, format="JPEG", exif=exif)
    source_b64 = base64.b64encode(source.getvalue()).decode()
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.secret_scrub_values.append(secret)
    ctx.delivered_unverified_observed_outputs = {"result": {"screenshot_base64": source_b64}}

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    published_b64 = observed["result"]["screenshot_base64"]
    published = base64.b64decode(published_b64, validate=True)
    assert published_b64 != source_b64
    assert secret.encode() not in published
    with Image.open(BytesIO(published)) as image:
        image.load()
        assert image.format == "JPEG"
        assert len(image.getexif()) == 0


def test_delivered_unverified_accepts_internal_named_fields_only_from_typed_public_output_source() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {
        "result": {
            "amount": 0,
            "browserSessionId": "browser_session_internal",
            "browser_session_id": "browser_session_internal_snake",
            "runSessionId": "run_session_internal",
            "run_session_id": "run_session_internal_snake",
        }
    }

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["result"] == {
        "amount": 0,
        "browserSessionId": "browser_session_internal",
        "browser_session_id": "browser_session_internal_snake",
        "runSessionId": "run_session_internal",
        "run_session_id": "run_session_internal_snake",
    }


def test_delivered_unverified_rejects_untyped_context_output_provenance() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {"result": {"amount": 0, "sessionToken": "internal"}}

    result = _make_agent_result(
        ctx,
        user_response="ok",
        updated_workflow=None,
        narrative_payload=_payload(),
    )

    assert result.narrative_payload is not None
    assert "deliveredUnverifiedObservedOutputs" not in result.narrative_payload


@pytest.mark.parametrize(
    ("malformed_image_prefix", "padding"),
    [
        pytest.param("iVBORw0KGgoAAAANSUhEUg", "==", id="png-header"),
        pytest.param("/9j/4AAQSkZJRgABAQAAAQABAAD", "=", id="jpeg-header"),
    ],
)
def test_delivered_unverified_backfill_omits_invalid_image_prefixed_base64(
    malformed_image_prefix: str,
    padding: str,
) -> None:
    secret = "registeredsecretvalu"
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.secret_scrub_values.append(secret)
    ctx.delivered_unverified_observed_outputs = {
        "result": {"screenshot_base64": malformed_image_prefix + secret + padding}
    }

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["result"]["screenshot_base64"] == {"$skyvernOmitted": {"reason": "invalid image", "count": 1}}
    assert observed["$skyvernOutput"]["omitted"]["image"] == 1
    assert secret not in str(result.narrative_payload)


def test_make_agent_result_omits_malformed_jpeg_that_retains_eoi() -> None:
    malformed_jpeg = base64.b64encode(
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"registered-secret-value" + b"\xff\xd9"
    ).decode()
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.secret_scrub_values.append("registered-secret-value")
    ctx.delivered_unverified_observed_outputs = {"result": {"screenshot_base64": malformed_jpeg}}

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["result"]["screenshot_base64"] == {"$skyvernOmitted": {"reason": "invalid image", "count": 1}}
    assert observed["$skyvernOutput"]["omitted"]["image"] == 1
    assert "registered-secret-value" not in str(observed)


def test_backfill_omits_delivered_unverified_outputs_for_non_delivered_terminal() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_observed_outputs = {"result": {"amount": 0}}

    result = _result(
        ctx,
        narrative_payload=_payload(deliveredUnverifiedObservedOutputs={"result": {"password": "caller-secret-bypass"}}),
    )

    assert result.narrative_payload is not None
    assert "deliveredUnverifiedObservedOutputs" not in result.narrative_payload
    assert "caller-secret-bypass" not in str(result.narrative_payload)


def test_backfill_omits_caller_delivered_unverified_outputs_without_context() -> None:
    result = _result(
        None,
        narrative_payload=_payload(deliveredUnverifiedObservedOutputs={"result": {"password": "caller-secret-bypass"}}),
    )

    assert result.narrative_payload is not None
    assert "deliveredUnverifiedObservedOutputs" not in result.narrative_payload
    assert "caller-secret-bypass" not in str(result.narrative_payload)


def test_backfill_replaces_caller_delivered_unverified_outputs_with_sanitized_snapshot() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.secret_scrub_values.append("canonical-secret")
    ctx.delivered_unverified_observed_outputs = {
        "result": {"amount": 0, "registered": "prefix canonical-secret suffix"}
    }
    caller_outputs = {"result": {"amount": 999, "password": "caller-secret-bypass"}}

    result = _result(
        ctx,
        narrative_payload=_payload(deliveredUnverifiedObservedOutputs=caller_outputs),
    )

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["result"] == {"amount": 0, "registered": "prefix [REDACTED_SECRET] suffix"}
    assert "caller-secret-bypass" not in str(result.narrative_payload)


def test_backfill_rejects_underscored_snapshot_keyword_override() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {"result": {"amount": 0}}

    result = _result(
        ctx,
        narrative_payload=_payload(),
        _delivered_unverified_snapshot={"result": {"password": "raw-snapshot-bypass"}},
    )

    assert result.narrative_payload is not None
    assert "deliveredUnverifiedObservedOutputs" not in result.narrative_payload
    assert "raw-snapshot-bypass" not in str(result.narrative_payload)


def test_backfill_rejects_public_outputs_keyword_override() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {"result": {"amount": 0}}

    result = _result(
        ctx,
        narrative_payload=_payload(),
        delivered_unverified_observed_outputs={"result": {"password": "raw-public-bypass"}},
    )

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["result"] == {"amount": 0}
    assert "raw-public-bypass" not in str(result.narrative_payload)


def test_backfill_removes_caller_delivered_unverified_outputs_when_snapshot_is_empty() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True

    result = _result(
        ctx,
        narrative_payload=_payload(deliveredUnverifiedObservedOutputs={"result": {"password": "caller-secret-bypass"}}),
    )

    assert result.narrative_payload is not None
    assert "deliveredUnverifiedObservedOutputs" not in result.narrative_payload
    assert "caller-secret-bypass" not in str(result.narrative_payload)


def test_backfill_redacts_sensitive_value_when_registered_secret_rewrites_source_key() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.secret_scrub_values.append("password")
    ctx.delivered_unverified_observed_outputs = {"password": "synthetic-unregistered-sensitive-value"}

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["[REDACTED_SECRET]"] == "****"
    assert "synthetic-unregistered-sensitive-value" not in str(result.narrative_payload)


def test_backfill_redacts_value_for_assignment_style_sensitive_source_key() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {"password=placeholder": "synthetic-unregistered-sensitive-value"}

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["[REDACTED_SECRET]"] == "****"
    assert "synthetic-unregistered-sensitive-value" not in str(result.narrative_payload)


def test_backfill_redacts_value_for_colon_assignment_sensitive_source_key() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {"password:placeholder": "synthetic-unregistered-sensitive-value"}

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["[REDACTED_SECRET]"] == "****"
    assert "synthetic-unregistered-sensitive-value" not in str(result.narrative_payload)


def test_backfill_redacts_camel_case_sensitive_source_keys() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {
        "sessionToken": "session-secret",
        "accessToken": "access-secret",
        "privateKey": "private-secret",
        "clientSecret": "client-secret",
        "IDToken": "id-secret",
        "JWTToken": "jwt-secret",
        "CSRFToken": "csrf-secret",
        "APISecret": "api-secret",
    }

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    sensitive_keys = (
        "sessionToken",
        "accessToken",
        "privateKey",
        "clientSecret",
        "IDToken",
        "JWTToken",
        "CSRFToken",
        "APISecret",
    )
    assert {key: observed[key] for key in sensitive_keys} == {
        "sessionToken": "****",
        "accessToken": "****",
        "privateKey": "****",
        "clientSecret": "****",
        "IDToken": "****",
        "JWTToken": "****",
        "CSRFToken": "****",
        "APISecret": "****",
    }
    for secret in (
        "session-secret",
        "access-secret",
        "private-secret",
        "client-secret",
        "id-secret",
        "jwt-secret",
        "csrf-secret",
        "api-secret",
    ):
        assert secret not in str(observed)


@pytest.mark.parametrize(
    "key",
    [
        "auth_token",
        "client_secret",
        "refresh_token",
        "x-api-key",
        "client-secret",
        "client secret",
        "db_password",
        "user_password",
        "bearer_token",
    ],
)
def test_backfill_redacts_delimited_sensitive_source_keys(key: str) -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {key: "opaque-secret-value"}

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed[key] == "****"
    assert "opaque-secret-value" not in str(observed)


@pytest.mark.parametrize(
    "key",
    [
        "pageToken",
        "page_token",
        "page-token",
        "nextToken",
        "next_token",
        "next-token",
        "credentialId",
        "credential_id",
        "credential-id",
        "author",
    ],
)
def test_delivered_unverified_sensitive_key_preserves_non_secret_delimiter_variants(key: str) -> None:
    assert agent_module._delivered_unverified_sensitive_key(key) is False


def test_backfill_disambiguates_captured_omission_marker_shape() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    captured_marker = {"reason": "string budget", "count": 1}
    ctx.delivered_unverified_observed_outputs = {"result": {"$skyvernOmitted": captured_marker}}

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["result"] == {"$skyvernOmitted [captured]": captured_marker}


def test_backfill_omits_internal_source_workflow_run_id_from_structured_output() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_workflow_run_id = "wr_internal_source"
    ctx.delivered_unverified_observed_outputs = {"result": {"amount": 0}}

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert "wr_internal_source" not in str(observed)
    assert "sourceWorkflowRunId" not in str(observed)


def test_long_ordinary_string_is_sanitized_without_structured_truncation() -> None:
    ctx = _ctx()
    budget = agent_module._DeliveredUnverifiedAdmissionBudget()
    captured = "x" * 600

    observed = agent_module._bounded_delivered_unverified_scalar(ctx, captured, budget)

    assert observed == captured
    assert budget.string_omitted == 0


def test_raw_key_budget_rejects_before_sensitive_key_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {"password=" + "x" * 513: "must-not-persist"}

    def fail_if_classified(_key: str) -> bool:
        raise AssertionError("oversized raw key reached sensitive-key classification")

    monkeypatch.setattr(agent_module, "_delivered_unverified_sensitive_key", fail_if_classified)

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed == {"$skyvernOutput": {"omitted": {"string": 1}}}
    assert "must-not-persist" not in str(observed)


def test_backfill_bounds_nodes_depth_strings_and_serialized_bytes_with_omission_accounting() -> None:
    deeply_nested: dict[str, Any] = {"secret": "must-not-survive"}
    for index in range(100):
        deeply_nested = {f"level_{index}": deeply_nested}

    cases = {
        "depth": {"deep": deeply_nested},
        "node": {"many": {f"field_{index}": "value" for index in range(10_000)}},
        "serializedBytes": {"oversized_string": "x" * 100_000},
    }
    for reason, captured in cases.items():
        ctx = _ctx()
        ctx.delivered_unverified_terminal = True
        ctx.delivered_unverified_workflow_run_id = f"wr_oversized_{reason}"
        ctx.delivered_unverified_observed_outputs = captured

        result = _result(ctx, narrative_payload=_payload())

        assert result.narrative_payload is not None
        observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
        metadata = observed["$skyvernOutput"]
        assert f"wr_oversized_{reason}" not in str(observed)
        assert metadata["omitted"][reason] >= 1
        assert len(json.dumps(observed, separators=(",", ":")).encode()) <= 65_536
        if reason == "depth":
            assert "must-not-survive" not in str(observed)


def test_delivered_unverified_node_budget_prioritizes_later_bounded_scalars_over_long_text() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {
        "result": {
            **{f"long_{index}": "x" * 512 for index in range(64)},
            "amount": 0,
            "confirmation": "later-confirmation",
        }
    }

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["result"]["amount"] == 0
    assert observed["result"]["confirmation"] == "later-confirmation"
    assert observed["$skyvernOutput"]["omitted"]["node"] >= 1


def test_delivered_unverified_structured_surface_preserves_ordinary_600_character_output() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    captured = "captured-code-output-" + "x" * 579
    ctx.delivered_unverified_observed_outputs = {"result": {"code_output": captured}}

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["result"]["code_output"] == captured


def test_delivered_unverified_serialized_byte_budget_retains_scalars_and_counts_omitted_paths() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {
        "amount": 0,
        "confirmation": "confirmed",
        "large": {f"field_{index}": "🧪" * 512 for index in range(64)},
    }

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["amount"] == 0
    assert observed["confirmation"] == "confirmed"
    assert observed["$skyvernOutput"]["omitted"]["serializedBytes"] >= 1
    assert len(json.dumps(observed, ensure_ascii=False, separators=(",", ":")).encode()) <= 65_536


def test_delivered_unverified_list_byte_budget_retains_prior_scalars_and_counts_omissions() -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {
        "amount": 0,
        "large": ["🧪" * 512 for _ in range(64)],
    }

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["amount"] == 0
    assert observed["$skyvernOutput"]["omitted"]["serializedBytes"] >= 1
    assert len(json.dumps(observed, separators=(",", ":")).encode()) <= 65_536


@pytest.mark.parametrize(
    "oversized_image",
    [
        pytest.param("data:image/png;base64," + "A" * 65_540, id="data-url"),
        pytest.param("iVBORw0KGgoAAAAN" + "A" * 65_540, id="bare-base64"),
    ],
)
def test_delivered_unverified_oversized_image_rejected_before_decode(
    monkeypatch: pytest.MonkeyPatch,
    oversized_image: str,
) -> None:
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.delivered_unverified_observed_outputs = {"result": {"image": oversized_image}}

    real_decode = agent_module.base64.b64decode

    def fail_decode(value: str, **kwargs: object) -> bytes:
        if len(value) > 16:
            raise AssertionError("oversized image reached base64 decode")
        return real_decode(value, **kwargs)

    monkeypatch.setattr(agent_module.base64, "b64decode", fail_decode)

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    observed = result.narrative_payload["deliveredUnverifiedObservedOutputs"]
    assert observed["result"]["image"] == {"$skyvernOmitted": {"reason": "invalid image", "count": 1}}


def test_delivered_unverified_image_pixel_limit_checked_before_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OversizedImage:
        size = (1_001, 1_000)
        format = "PNG"

        def __enter__(self) -> OversizedImage:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def convert(self, _mode: str) -> None:
            raise AssertionError("oversized image reached pixel conversion")

    monkeypatch.setattr(agent_module.Image, "open", lambda _stream: OversizedImage())
    encoded = base64.b64encode(agent_module._PNG_START + agent_module._PNG_END).decode()

    image_input, canonical = agent_module._canonical_delivered_unverified_image(encoded)

    assert image_input is True
    assert canonical is None


def test_delivered_unverified_image_data_url_is_canonicalized_without_metadata() -> None:
    secret = "data-url-secret"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("comment", secret)
    source = BytesIO()
    Image.new("RGB", (1, 1), (12, 34, 56)).save(source, format="PNG", pnginfo=metadata)
    ctx = _ctx()
    ctx.delivered_unverified_terminal = True
    ctx.secret_scrub_values.append(secret)
    ctx.delivered_unverified_observed_outputs = {
        "result": {"image": "data:image/png;base64," + base64.b64encode(source.getvalue()).decode()}
    }

    result = _result(ctx, narrative_payload=_payload())

    assert result.narrative_payload is not None
    published = base64.b64decode(
        result.narrative_payload["deliveredUnverifiedObservedOutputs"]["result"]["image"], validate=True
    )
    assert secret.encode() not in published


def test_make_agent_result_records_resolved_credentials_as_durable_approval() -> None:
    ctx = _ctx(request_policy=RequestPolicy(resolved_credentials=[SimpleNamespace(credential_id="cred_portal")]))

    result = _result(ctx, narrative_payload=_payload())

    approved = StructuredContext.from_json_str(result.global_llm_context).approved_credentials
    assert [record.credential_id for record in approved] == ["cred_portal"]
