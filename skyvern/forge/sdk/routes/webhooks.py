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
    try:
        return await build_run_preview(
            organization_id=current_org.organization_id,
            run_id=run_id,
        )
    except MissingApiKey as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except (TaskNotFound, WorkflowRunNotFound) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message or str(exc))
    except WebhookReplayError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except SkyvernHTTPException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
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
    try:
        return await replay_run_webhook(
            organization_id=current_org.organization_id,
            run_id=run_id,
            target_url=request.override_webhook_url,
        )
    except MissingWebhookTarget as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except MissingApiKey as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except (TaskNotFound, WorkflowRunNotFound) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message or str(exc))
    except WebhookReplayError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except SkyvernHTTPException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:  # pragma: no cover - defensive guard
        LOG.error("Failed to replay webhook", run_id=run_id, error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to replay webhook.",
        ) from exc
