from time import perf_counter

import httpx
import structlog
from fastapi import Depends

from skyvern.exceptions import BlockedHost, SkyvernHTTPException
from skyvern.forge import app
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_headers
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.webhooks import TestWebhookRequest, TestWebhookResponse
from skyvern.services.webhook_service import build_sample_task_payload, build_sample_workflow_run_payload
from skyvern.utils.url_validators import validate_url

LOG = structlog.get_logger()


@legacy_base_router.post(
    "/internal/test-webhook",
    tags=["Internal"],
    description="Test a webhook endpoint by sending a sample payload",
    summary="Test webhook endpoint",
)
@base_router.post(
    "/internal/test-webhook",
    tags=["Internal"],
    description="Test a webhook endpoint by sending a sample payload",
    summary="Test webhook endpoint",
)
async def test_webhook(
    request: TestWebhookRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> TestWebhookResponse:
    """
    Test a webhook endpoint by sending a sample signed payload.

    This endpoint allows users to:
    - Validate their webhook receiver can be reached
    - Test HMAC signature verification
    - See the exact headers and payload format Skyvern sends

    The endpoint respects SSRF protection (BLOCKED_HOSTS, private IPs) and will return
    a helpful error message if the URL is blocked.
    """
    start_time = perf_counter()

    # Validate the URL (raises BlockedHost or SkyvernHTTPException for invalid URLs)
    try:
        validated_url = validate_url(request.webhook_url)
        if not validated_url:
            return TestWebhookResponse(
                status_code=None,
                latency_ms=0,
                response_body="",
                headers_sent={},
                error="Invalid webhook URL",
            )
    except BlockedHost as exc:
        return TestWebhookResponse(
            status_code=None,
            latency_ms=0,
            response_body="",
            headers_sent={},
            error=(
                f"This URL is blocked by SSRF protection (host: {exc.host}). "
                "Add the host to ALLOWED_HOSTS to test internal endpoints or use an external receiver "
                "such as webhook.site or requestbin.com."
            ),
        )
    except SkyvernHTTPException as exc:
        error_message = getattr(exc, "message", None) or "Invalid webhook URL. Use http(s) and a valid host."
        return TestWebhookResponse(
            status_code=None,
            latency_ms=0,
            response_body="",
            headers_sent={},
            error=error_message,
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        LOG.error("Error validating webhook URL", error=str(exc), webhook_url=request.webhook_url)
        return TestWebhookResponse(
            status_code=None,
            latency_ms=0,
            response_body="",
            headers_sent={},
            error="Unexpected error while validating the webhook URL.",
        )

    # Build the sample payload based on run type
    try:
        if request.run_type == "task":
            payload = build_sample_task_payload(run_id=request.run_id)
        else:  # workflow_run
            payload = build_sample_workflow_run_payload(run_id=request.run_id)
    except Exception as e:
        LOG.error("Error building sample payload", error=str(e), run_type=request.run_type)
        return TestWebhookResponse(
            status_code=None,
            latency_ms=0,
            response_body="",
            headers_sent={},
            error=f"Failed to build sample payload: {str(e)}",
        )

    # Get the organization's API key to sign the webhook
    # For testing, we use a placeholder if no API key is available
    api_key_obj = await app.DATABASE.get_valid_org_auth_token(
        current_org.organization_id,
        OrganizationAuthTokenType.api.value,
    )
    api_key = api_key_obj.token if api_key_obj else "test_api_key_placeholder"

    headers = generate_skyvern_webhook_headers(payload=payload, api_key=api_key)

    # Send the webhook request
    status_code = None
    response_body = ""
    error = None

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                validated_url,
                content=payload,
                headers=headers,
                timeout=httpx.Timeout(10.0),
            )
            status_code = response.status_code

            # Capture first 2KB of response body
            response_text = response.text
            if len(response_text) > 2048:
                response_body = response_text[:2048] + "\n... (truncated)"
            else:
                response_body = response_text

    except httpx.TimeoutException:
        error = "Request timed out after 10 seconds."
        LOG.warning(
            "Test webhook timeout",
            organization_id=current_org.organization_id,
            webhook_url=validated_url,
        )
    except httpx.NetworkError as exc:
        error = f"Could not reach URL: {exc}"
        LOG.warning(
            "Test webhook network error",
            organization_id=current_org.organization_id,
            webhook_url=validated_url,
            error=str(exc),
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        error = f"Unexpected error: {exc}"
        LOG.error(
            "Test webhook unexpected error",
            organization_id=current_org.organization_id,
            webhook_url=validated_url,
            error=str(exc),
            exc_info=True,
        )

    latency_ms = int((perf_counter() - start_time) * 1000)

    return TestWebhookResponse(
        status_code=status_code,
        latency_ms=latency_ms,
        response_body=response_body,
        headers_sent=headers,
        error=error,
    )
