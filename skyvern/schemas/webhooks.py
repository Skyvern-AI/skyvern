from typing import Literal

from pydantic import BaseModel, Field


class TestWebhookRequest(BaseModel):
    webhook_url: str = Field(..., description="The webhook URL to test")
    run_type: Literal["task", "workflow_run"] = Field(..., description="Type of run to simulate")
    run_id: str | None = Field(None, description="Optional run ID to include in the sample payload")


class TestWebhookResponse(BaseModel):
    status_code: int | None = Field(None, description="HTTP status code from the webhook receiver")
    latency_ms: int = Field(..., description="Round-trip time in milliseconds")
    response_body: str = Field(..., description="First 2KB of the response body")
    headers_sent: dict[str, str] = Field(..., description="Headers sent with the webhook request")
    error: str | None = Field(None, description="Error message if the request failed")
