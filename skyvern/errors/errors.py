from pydantic import BaseModel, Field


class UserDefinedError(BaseModel):
    error_code: str
    reasoning: str
    confidence_float: float = Field(..., ge=0, le=1)

    def __repr__(self) -> str:
        return f"{self.reasoning}(error_code={self.error_code}, confidence_float={self.confidence_float})"


class SkyvernDefinedError(BaseModel):
    error_code: str
    reasoning: str

    def __repr__(self) -> str:
        return f"{self.reasoning}(error_code={self.error_code})"

    def to_user_defined_error(self) -> UserDefinedError:
        return UserDefinedError(error_code=self.error_code, reasoning=self.reasoning, confidence_float=1.0)


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
