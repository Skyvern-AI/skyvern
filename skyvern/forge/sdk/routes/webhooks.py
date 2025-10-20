import structlog
from fastapi import Depends, HTTPException, status

from skyvern.exceptions import SkyvernHTTPException, TaskNotFound, WorkflowRunNotFound
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.webhooks import (
    RunWebhookPreviewResponse,
    RunWebhookReplayRequest,
    RunWebhookReplayResponse,
)
from skyvern.services.webhook_service import (
    MissingApiKey,
    MissingWebhookTarget,
    WebhookReplayError,
    build_run_preview,
    replay_run_webhook,
)

LOG = structlog.get_logger()


@legacy_base_router.get(
    "/internal/runs/{run_id}/test-webhook",
    tags=["Internal"],
    response_model=RunWebhookPreviewResponse,
    summary="Preview webhook replay payload",
)
@base_router.get(
    "/internal/runs/{run_id}/test-webhook",
    tags=["Internal"],
    response_model=RunWebhookPreviewResponse,
    summary="Preview webhook replay payload",
)
async def preview_webhook_replay(
    run_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
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
)
@base_router.post(
    "/internal/runs/{run_id}/test-webhook",
    tags=["Internal"],
    response_model=RunWebhookReplayResponse,
    summary="Replay webhook for a completed run",
)
async def trigger_webhook_replay(
    run_id: str,
    request: RunWebhookReplayRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
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
