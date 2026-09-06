"""Tests for the UserDefinedError / SkyvernDefinedError field contracts:
the error_type discriminator (SKY-7619) and the reasoning length bound (SKY-15620).
"""

import pytest
from pydantic import ValidationError

from skyvern.constants import ERROR_CODE_REASONING_MAX_LENGTH
from skyvern.errors.errors import (
    ErrorType,
    ReachMaxStepsError,
    SkyvernDefinedError,
    UserDefinedError,
)


def test_user_defined_error_has_user_error_type() -> None:
    error = UserDefinedError(error_code="invalid_credentials", reasoning="Bad creds", confidence_float=1.0)
    assert error.error_type == ErrorType.USER_DEFINED_ERROR


def test_user_defined_error_type_serialized_in_model_dump() -> None:
    error = UserDefinedError(error_code="invalid_credentials", reasoning="Bad creds", confidence_float=1.0)
    dumped = error.model_dump()
    assert dumped["error_type"] == "USER_DEFINED_ERROR"


def test_skyvern_defined_error_has_system_error_type() -> None:
    error = SkyvernDefinedError(error_code="REACH_MAX_STEPS", reasoning="Max steps reached.")
    assert error.error_type == ErrorType.SYSTEM_DEFINED_ERROR


def test_skyvern_defined_error_type_serialized_in_model_dump() -> None:
    error = ReachMaxStepsError()
    dumped = error.model_dump()
    assert dumped["error_type"] == "SYSTEM_DEFINED_ERROR"


def test_to_user_defined_error_inherits_user_error_type() -> None:
    skyvern_error = ReachMaxStepsError()
    user_error = skyvern_error.to_user_defined_error()
    assert user_error.error_type == ErrorType.USER_DEFINED_ERROR
    assert user_error.model_dump()["error_type"] == "USER_DEFINED_ERROR"


def test_reasoning_longer_than_the_bound_is_truncated_at_construction() -> None:
    # reasoning is aggregated into tasks.errors and leaves over the customer webhook, and that
    # column APPENDS rather than replaces, so an unbounded string accumulates.
    error = UserDefinedError(
        error_code="COVERAGE_NOT_ACTIVE",
        reasoning="z" * (ERROR_CODE_REASONING_MAX_LENGTH + 2500),
        confidence_float=1.0,
    )
    assert len(error.reasoning) == ERROR_CODE_REASONING_MAX_LENGTH


def test_reasoning_at_or_below_the_bound_is_byte_identical() -> None:
    # The bound must be a no-op on the 99.99923% of rows that already fit; pooled p99 is 307 chars
    # (v3 alone is p99 255, max 634).
    exact = "y" * ERROR_CODE_REASONING_MAX_LENGTH
    assert UserDefinedError(error_code="A", reasoning=exact, confidence_float=1.0).reasoning == exact
    short = "the portal rejected the submission"
    assert UserDefinedError(error_code="A", reasoning=short, confidence_float=1.0).reasoning == short


def test_the_bound_covers_the_skyvern_defined_conversion() -> None:
    # to_user_defined_error is one of the six construction sites; a bound that only covered direct
    # construction would leave it open.
    converted = SkyvernDefinedError(
        error_code="REACH_MAX_STEPS", reasoning="q" * (ERROR_CODE_REASONING_MAX_LENGTH + 900)
    ).to_user_defined_error()
    assert len(converted.reasoning) == ERROR_CODE_REASONING_MAX_LENGTH


def test_reasoning_still_rejects_a_non_string() -> None:
    # The bound must not weaken the field into accepting anything it previously refused.
    with pytest.raises(ValidationError):
        UserDefinedError(error_code="A", reasoning=None, confidence_float=1.0)  # type: ignore[arg-type]


def test_bounding_a_redacted_reasoning_cannot_reexpose_the_secret() -> None:
    # The safety argument for putting the bound on the model: where redaction runs at all, it runs
    # BEFORE construction, and truncating already-redacted text cannot un-redact it.
    # This pins the safe half ONLY. At the sites that construct from raw text and never redact, the
    # model's cut now lands UPSTREAM of the read-time redactor in _merge_workflow_run_errors
    # (workflow/service.py, which redacts then bounds at 2000), so a secret straddling char 1000
    # survives there as a fragment. This bound does not create that exposure -- the task webhook
    # already ships the value whole -- it narrows what the aggregator can scrub. Closure: SKY-15640.
    from skyvern.utils.secret_redaction import redact_secrets_from_text

    secret = "sk4829137765"
    raw = "a" * (ERROR_CODE_REASONING_MAX_LENGTH - 5) + secret + "b" * 3000
    redacted = redact_secrets_from_text(raw, {secret})
    assert secret not in redacted

    error = UserDefinedError(error_code="A", reasoning=redacted, confidence_float=1.0)
    assert len(error.reasoning) == ERROR_CODE_REASONING_MAX_LENGTH
    assert secret not in error.reasoning


def test_a_cut_landing_on_whitespace_still_produces_a_strip_clean_reasoning() -> None:
    # _strict_user_defined_error_payload drops any error whose reasoning is not strip()-clean, so a
    # cut landing on a space would discard the whole row -- losing the error_code, not just the
    # tail. Not hypothetical: of the rows a cut would truncate, one lands on a space.
    landing_on_space = "a" * (ERROR_CODE_REASONING_MAX_LENGTH - 1) + "  tail beyond the bound"
    error = UserDefinedError(error_code="A", reasoning=landing_on_space, confidence_float=1.0)
    assert error.reasoning == error.reasoning.strip()
    assert len(error.reasoning) == ERROR_CODE_REASONING_MAX_LENGTH - 1


def test_an_all_whitespace_slice_is_not_rstripped_into_an_empty_reasoning() -> None:
    # _strict_user_defined_error_payload drops an EMPTY reasoning as readily as a non-strip-clean
    # one, so rstripping a whitespace-only slice to "" would lose the error_code that truncating is
    # supposed to preserve. Fall back to the un-stripped slice instead.
    whitespace_prefix = " " * ERROR_CODE_REASONING_MAX_LENGTH + "the real narration starts here"
    error = UserDefinedError(error_code="A", reasoning=whitespace_prefix, confidence_float=1.0)
    assert error.reasoning != ""
    assert len(error.reasoning) == ERROR_CODE_REASONING_MAX_LENGTH


def test_the_bound_admits_exactly_what_the_declared_gate_admits() -> None:
    # The reasoning length contract is declared once, in skyvern.schemas.workflows, and the ingress
    # validators gate on it. A second, stricter number on the model would silently drop characters a
    # workflow author had just been told were acceptable, so the model must accept the full gate
    # width untouched. (The cloud-side runtime parity for the same constant is asserted in
    # tests/cloud/test_code_block_escalation_secure.py, which can import the cloud package.)
    at_the_gate = "y" * ERROR_CODE_REASONING_MAX_LENGTH
    error = UserDefinedError(error_code="A", reasoning=at_the_gate, confidence_float=1.0)
    assert error.reasoning == at_the_gate
    assert len(error.reasoning) == ERROR_CODE_REASONING_MAX_LENGTH
