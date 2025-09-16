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


class ReachMaxStepsError(SkyvernDefinedError):
    error_code: str = "REACH_MAX_STEPS"
    reasoning: str = "The agent has reached the maximum number of steps."


class ReachMaxRetriesError(SkyvernDefinedError):
    error_code: str = "REACH_MAX_RETRIES"
    reasoning: str = "The agent has reached the maximum number of retries. It might be an issue with the agent. Please reach out to the Skyvern team for support."
