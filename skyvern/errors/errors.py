from pydantic import BaseModel


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
