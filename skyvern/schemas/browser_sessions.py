from pydantic import BaseModel, Field

MIN_TIMEOUT = 5
MAX_TIMEOUT = 120
DEFAULT_TIMEOUT = 60


class CreateBrowserSessionRequest(BaseModel):
    timeout: int | None = Field(
        default=DEFAULT_TIMEOUT,
        description=f"Timeout in minutes for the session. Timeout is applied after the session is started. Must be between {MIN_TIMEOUT} and {MAX_TIMEOUT}. Defaults to {DEFAULT_TIMEOUT}.",
        ge=MIN_TIMEOUT,
        le=MAX_TIMEOUT,
    )
