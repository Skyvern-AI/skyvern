from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator

from skyvern.constants import ERROR_CODE_REASONING_MAX_LENGTH

CREDENTIAL_SITE_MESSAGE_MAX_LENGTH = 300


class ErrorType(StrEnum):
    USER_DEFINED_ERROR = "USER_DEFINED_ERROR"
    SYSTEM_DEFINED_ERROR = "SYSTEM_DEFINED_ERROR"


class CredentialCheck(StrEnum):
    """The confirmations a credential-rejection verdict has to clear before it is returned as one."""

    SITE_REJECTED_CREDENTIALS = "site_rejected_credentials"
    NO_RECENT_LOGIN_WITH_SAME_CREDENTIAL = "no_recent_login_with_same_credential"
    LOGIN_REQUEST_NOT_UNAVAILABLE = "login_request_not_5xx_or_429"


class CredentialCheckEvidence(BaseModel):
    """Why a credential-rejection verdict was or was not confirmed. Reaches the caller on the run
    response and the webhook, so a customer can tell a confirmed rejection from an unconfirmed one
    before blocking the credential."""

    confirmed: bool = Field(
        description=(
            "True only when every credential confirmation check passed. The login-request status check is not "
            "available yet, so the organization-scoped confirmation flag must remain disabled until that check "
            "is implemented."
        )
    )
    checks_passed: list[CredentialCheck] = Field(default_factory=list)
    checks_failed: list[CredentialCheck] = Field(default_factory=list)
    # A check with no evidence to read is neither passed nor failed; without this a caller cannot
    # tell "the site said nothing about the credentials" from "we never looked".
    checks_skipped: list[CredentialCheck] = Field(default_factory=list)
    site_message: str | None = None
    login_request_status: int | None = None

    @field_validator("site_message")
    @classmethod
    def _bound_site_message(cls, value: str | None) -> str | None:
        # Quoted from the page by a model, so it is bounded here for the same reason `reasoning` is:
        # it leaves over the customer webhook into a column that appends.
        if value is None or len(value) <= CREDENTIAL_SITE_MESSAGE_MAX_LENGTH:
            return value
        cut = value[:CREDENTIAL_SITE_MESSAGE_MAX_LENGTH]
        return cut.rstrip() or cut


class UserDefinedError(BaseModel):
    error_code: str
    reasoning: str
    confidence_float: float = Field(..., ge=0, le=1)
    error_type: Literal[ErrorType.USER_DEFINED_ERROR] = ErrorType.USER_DEFINED_ERROR
    # Request input is always discarded; only with_credential_check attaches gate-generated evidence.
    credential_check: CredentialCheckEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Confirmation evidence set by Skyvern; request input is ignored. This field remains unconfirmed "
            "until all required checks, including login-request status, are available."
        ),
    )
    # Private so no LLM or API payload can set it; only SkyvernDefinedError.to_user_defined_error does.
    _skyvern_defined: bool = PrivateAttr(default=False)
    # Private for the same reason: it stops the confirmation gate from paying for a second model call
    # when two detection paths hand it the same error, and a model must not be able to claim it.
    _credential_check_done: bool = PrivateAttr(default=False)

    @property
    def is_skyvern_defined(self) -> bool:
        return self._skyvern_defined

    @property
    def is_credential_check_done(self) -> bool:
        return self._credential_check_done

    @field_validator("credential_check", mode="before")
    @classmethod
    def _drop_untrusted_credential_check(cls, value: object) -> None:
        # Only with_credential_check may attach this evidence; it uses model_copy after validation.
        return None

    def with_credential_check(self, evidence: CredentialCheckEvidence) -> "UserDefinedError":
        """A copy carrying the confirmation outcome, marked so the gate will not re-run on it."""
        copied = self.model_copy(update={"credential_check": evidence})
        copied._credential_check_done = True
        return copied

    def without_unverified_credential_check(self) -> "UserDefinedError":
        """Drops a `credential_check` the confirmation gate did not write. The field parses like any
        other, so a model echoing it into its errors array would otherwise pass its own claim off as
        Skyvern's evidence."""
        if self._credential_check_done or self.credential_check is None:
            return self
        return self.model_copy(update={"credential_check": None})

    @field_validator("reasoning")
    @classmethod
    def _bound_reasoning(cls, value: str) -> str:
        # reasoning is LLM-authored and reaches the customer over the task webhook, in a column that
        # appends rather than replaces. Truncate rather than reject: dropping the error would lose
        # the error_code, which is the half callers act on, to save the narration tail.
        # rstrip because _strict_user_defined_error_payload drops any reasoning that is not
        # strip()-clean, so a cut landing on whitespace would discard the whole row.
        # NOT a universal guarantee: model_copy(update=...) skips field validators, so a caller
        # rebuilding reasoning that way must bound it itself.
        if len(value) <= ERROR_CODE_REASONING_MAX_LENGTH:
            return value
        cut = value[:ERROR_CODE_REASONING_MAX_LENGTH]
        # Fall back to the un-stripped slice rather than emptying it: the payload check drops an
        # empty reasoning too, which loses the error_code this truncation exists to preserve.
        return cut.rstrip() or cut

    def __repr__(self) -> str:
        return f"{self.reasoning}(error_code={self.error_code}, confidence_float={self.confidence_float})"


def filter_to_user_defined_codes(
    errors: list[UserDefinedError],
    error_code_mapping: dict[str, str] | None,
) -> tuple[list[UserDefinedError], list[str]]:
    """Drop LLM-returned errors whose code is not a key in error_code_mapping.

    LLM-based error surfacing prompts occasionally hallucinate codes from the
    failure_categories taxonomy (e.g. LLM_REASONING_ERROR) and place them in
    the user-defined errors field. Returns (kept, dropped_codes) so callers
    can log the drop with their own task/step context.
    """
    if not error_code_mapping:
        skyvern_errors = [error for error in errors if error.is_skyvern_defined]
        return skyvern_errors, [error.error_code for error in errors if not error.is_skyvern_defined]
    allowed = set(error_code_mapping.keys())
    kept: list[UserDefinedError] = []
    dropped: list[str] = []
    for error in errors:
        if error.is_skyvern_defined or error.error_code in allowed:
            kept.append(error)
        else:
            dropped.append(error.error_code)
    return kept, dropped


def resolve_error_code_mapping_key(code: object, error_code_mapping: dict[str, str] | None) -> str | None:
    """Return the error_code_mapping key that `code` names: an exact match, else the only case-insensitive one."""
    if not isinstance(code, str) or not error_code_mapping:
        return None
    if code in error_code_mapping:
        return code
    matches = [key for key in error_code_mapping if key.lower() == code.lower()]
    return matches[0] if len(matches) == 1 else None


class SkyvernDefinedError(BaseModel):
    error_code: str
    reasoning: str
    error_type: Literal[ErrorType.SYSTEM_DEFINED_ERROR] = ErrorType.SYSTEM_DEFINED_ERROR

    def __repr__(self) -> str:
        return f"{self.reasoning}(error_code={self.error_code})"

    def to_user_defined_error(self) -> UserDefinedError:
        error = UserDefinedError(error_code=self.error_code, reasoning=self.reasoning, confidence_float=1.0)
        error._skyvern_defined = True
        return error


class ReachMaxStepsError(SkyvernDefinedError):
    error_code: str = "REACH_MAX_STEPS"
    reasoning: str = "The agent has reached the maximum number of steps."


class ReachMaxRetriesError(SkyvernDefinedError):
    error_code: str = "REACH_MAX_RETRIES"
    reasoning: str = "The agent has reached the maximum number of retries. It might be an issue with the agent. Please reach out to the Skyvern team for support."


class GetTOTPVerificationCodeError(SkyvernDefinedError):
    error_code: str = "OTP_ERROR"
    reasoning: str = (
        "Failed to get TOTP verification code. Please confirm the TOTP functionality is working correctly on your side."
    )

    def __init__(self, *, reason: str | None = None) -> None:
        reasoning = f"Failed to get TOTP verification code. Reason: {reason}" if reason else self.reasoning
        super().__init__(reasoning=reasoning)


class TimeoutGetTOTPVerificationCodeError(SkyvernDefinedError):
    error_code: str = "OTP_TIMEOUT"
    reasoning: str = "Timeout getting TOTP verification code."


class TOTPExpiredError(SkyvernDefinedError):
    error_code: str = "OTP_EXPIRED"
    reasoning: str = "TOTP verification code has expired during multi-field input sequence."


class MissingTOTPSourceError(SkyvernDefinedError):
    error_code: str = "MISSING_TOTP_SOURCE"
    reasoning: str = (
        "MFA was required but no TOTP source is configured. Configure one of: a totp_verification_url or "
        "totp_identifier on the login block (or run override), or attach a credential parameter whose "
        "credential stores a TOTP secret."
    )


_LOGIN_UNCONFIRMED_REASONING = (
    "The login did not succeed, but Skyvern could not confirm that the website evaluated the submitted "
    "username or password and rejected them. Treat this as an unconfirmed login failure rather than a "
    "bad credential."
)


class LoginUnconfirmedError(SkyvernDefinedError):
    """Replaces a credential-rejection code that failed confirmation. Customers block a credential on
    an `invalid_credentials` verdict, so a verdict Skyvern cannot stand behind gets its own code."""

    error_code: str = "CREDENTIAL_LOGIN_UNCONFIRMED"
    reasoning: str = _LOGIN_UNCONFIRMED_REASONING

    def __init__(self, *, reason: str | None = None) -> None:
        reasoning = f"{_LOGIN_UNCONFIRMED_REASONING} Reason: {reason}" if reason else _LOGIN_UNCONFIRMED_REASONING
        super().__init__(reasoning=reasoning)


_ACCOUNT_LOCKED_REASONING = (
    "The website reported that the account is locked, disabled, expired, or needs a password reset. The "
    "submitted username and password were not themselves rejected."
)


class AccountLockedError(SkyvernDefinedError):
    error_code: str = "CREDENTIAL_ACCOUNT_LOCKED"
    reasoning: str = _ACCOUNT_LOCKED_REASONING

    def __init__(self, *, reason: str | None = None) -> None:
        reasoning = f"{_ACCOUNT_LOCKED_REASONING} Reason: {reason}" if reason else _ACCOUNT_LOCKED_REASONING
        super().__init__(reasoning=reasoning)
