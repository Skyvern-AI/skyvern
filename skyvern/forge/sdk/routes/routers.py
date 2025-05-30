from fastapi import APIRouter

from skyvern.forge.sdk.api.workflow_scheduler import router as workflow_scheduler_router

base_router = APIRouter()
legacy_base_router = APIRouter(include_in_schema=False)
legacy_v2_router = APIRouter(include_in_schema=False)

# Register the workflow scheduler router
base_router.include_router(workflow_scheduler_router)
