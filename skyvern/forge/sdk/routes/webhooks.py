from time import perf_counter

import httpx
import structlog
from fastapi import Depends, HTTPException, status

from skyvern.exceptions import (
    BlockedHost,
    MissingApiKey,
    MissingWebhookTarget,
    SkyvernHTTPException,
    TaskNotFound,
    WebhookReplayError,
    WorkflowRunNotFound,
)
from skyvern.forge import app
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.webhooks import (
    RunWebhookPreviewResponse,
    RunWebhookReplayRequest,
    RunWebhookReplayResponse,
    TestWebhookRequest,
    TestWebhookResponse,
)
from skyvern.services.webhook_service import (
    build_run_preview,
    build_sample_task_payload,
    build_sample_workflow_run_payload,
    replay_run_webhook,
)
from skyvern.utils.url_validators import validate_url

LOG = structlog.get_logger()


@legacy_base_router.post(
    "/internal/test-webhook",
    tags=["Internal"],
    description="Test a webhook endpoint by sending a sample payload",
    summary="Test webhook endpoint",
    include_in_schema=False,
)
@base_router.post(
    "/internal/test-webhook",
    tags=["Internal"],
    description="Test a webhook endpoint by sending a sample payload",
    summary="Test webhook endpoint",
    include_in_schema=False,
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
        blocked_host: str | None = getattr(exc, "host", None)
        return TestWebhookResponse(
            status_code=None,
            latency_ms=0,
            response_body="",
            headers_sent={},
            error=(
                f"This URL is blocked by SSRF protection (host: {blocked_host or 'unknown'}). "
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
        LOG.exception("Error validating webhook URL", error=str(exc), webhook_url=request.webhook_url)
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
        LOG.exception("Error building sample payload", error=str(e), run_type=request.run_type)
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

    signed_data = generate_skyvern_webhook_signature(payload=payload, api_key=api_key)

    # Send the webhook request
    status_code = None
    response_body = ""
    error = None

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                validated_url,
                content=signed_data.signed_payload,
                headers=signed_data.headers,
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
        headers_sent=signed_data.headers,
        error=error,
    )


@legacy_base_router.get(
    "/internal/runs/{run_id}/test-webhook",
    tags=["Internal"],
    response_model=RunWebhookPreviewResponse,
    summary="Preview webhook replay payload",
    include_in_schema=False,
)
@base_router.get(
    "/internal/runs/{run_id}/test-webhook",
    tags=["Internal"],
    response_model=RunWebhookPreviewResponse,
    summary="Preview webhook replay payload",
    include_in_schema=False,
)
async def preview_webhook_replay(
    run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),  # noqa: B008
) -> RunWebhookPreviewResponse:
    """Return the replay payload preview for a completed run.

    Args:
        run_id (str): Identifier of the run to preview.
        current_org (Organization): Organization context for permission and signing.

    Returns:
        RunWebhookPreviewResponse: Payload and headers that would be used to replay the webhook.

    Raises:
        HTTPException: 400 if the organization lacks a valid API key or other replay preconditions fail.
        HTTPException: 404 if the specified run cannot be found.
        HTTPException: 500 if an unexpected error occurs while building the preview.
    """
    try:
        return await build_run_preview(
            organization_id=current_org.organization_id,
            run_id=run_id,
        )
    except MissingApiKey as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except (TaskNotFound, WorkflowRunNotFound) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except WebhookReplayError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SkyvernHTTPException as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive guard
        LOG.error("Failed to build webhook replay preview", run_id=run_id, error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build webhook replay preview.",
        ) from exc


@legacy_base_router.post(
    "/internal/runs/{run_id}/test-webhook",
    tags=["Internal"],
    response_model=RunWebhookReplayResponse,
    summary="Replay webhook for a completed run",
    include_in_schema=False,
)
@base_router.post(
    "/internal/runs/{run_id}/test-webhook",
    tags=["Internal"],
    response_model=RunWebhookReplayResponse,
    summary="Replay webhook for a completed run",
    include_in_schema=False,
)
async def trigger_webhook_replay(
    run_id: str,
    request: RunWebhookReplayRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),  # noqa: B008
) -> RunWebhookReplayResponse:
    """Replay a completed run's webhook to the stored or override URL.

    Args:
        run_id (str): Identifier of the run whose webhook should be replayed.
        request (RunWebhookReplayRequest): Optional override URL details for the replay.
        current_org (Organization): Organization context for permission and signing.

    Returns:
        RunWebhookReplayResponse: Delivery status information for the replay attempt.

    Raises:
        HTTPException: 400 if no target URL is available, the organization lacks an API key, or replay validation fails.
        HTTPException: 404 if the specified run cannot be found.
        HTTPException: 500 if an unexpected error occurs while replaying the webhook.
    """
    try:
        return await replay_run_webhook(
            organization_id=current_org.organization_id,
            run_id=run_id,
            target_url=request.override_webhook_url,
        )
    except MissingWebhookTarget as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except MissingApiKey as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except (TaskNotFound, WorkflowRunNotFound) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except WebhookReplayError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SkyvernHTTPException as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive guard
        LOG.error("Failed to replay webhook", run_id=run_id, error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to replay webhook.",
        ) from exc
