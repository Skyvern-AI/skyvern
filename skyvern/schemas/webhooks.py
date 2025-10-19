from pydantic import BaseModel, Field


class RunWebhookPreviewResponse(BaseModel):
    run_id: str = Field(..., description="Identifier of the run whose payload is being replayed")
    run_type: str = Field(..., description="Run type associated with the payload")
    default_webhook_url: str | None = Field(None, description="Webhook URL stored on the original run configuration")
    payload: str = Field(..., description="JSON payload that was delivered when the run completed")
    headers: dict[str, str] = Field(..., description="Signed headers that would accompany the replayed webhook")


class RunWebhookReplayRequest(BaseModel):
    override_webhook_url: str | None = Field(
        None,
        description="Optional webhook URL to send the payload to instead of the stored configuration",
    )


class RunWebhookReplayResponse(BaseModel):
    run_id: str = Field(..., description="Identifier of the run that was replayed")
    run_type: str = Field(..., description="Run type associated with the payload")
    default_webhook_url: str | None = Field(None, description="Webhook URL stored on the original run configuration")
    target_webhook_url: str | None = Field(None, description="Webhook URL that the replay attempted to reach")
    payload: str = Field(..., description="JSON payload that was delivered during the replay attempt")
    headers: dict[str, str] = Field(..., description="Signed headers that were generated for the replay attempt")
    status_code: int | None = Field(None, description="HTTP status code returned by the webhook receiver, if available")
    latency_ms: int | None = Field(None, description="Round-trip latency in milliseconds for the replay attempt")
    response_body: str | None = Field(None, description="Body returned by the webhook receiver (truncated to 2KB)")
    error: str | None = Field(None, description="Error message if the replay attempt failed")
