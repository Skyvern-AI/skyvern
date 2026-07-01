from enum import StrEnum

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry, LLMConfigRegistrationIssue
from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMConfigError
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.routes.internal_auth import _require_local_access
from skyvern.forge.sdk.schemas.custom_llms import CustomLLMConfig
from skyvern.forge.sdk.services.local_org_auth_token_service import SKYVERN_LOCAL_DOMAIN

router = APIRouter(prefix="/internal/llms", tags=["internal"])
LOG = structlog.get_logger()


class LLMDiagnosticsStatus(StrEnum):
    ok = "ok"
    setup_required = "setup_required"


class LLMConfigIssueResponse(BaseModel):
    llm_key: str
    missing_env_vars: list[str]
    detail: str


class LLMDiagnosticsResponse(BaseModel):
    status: LLMDiagnosticsStatus
    default_llm_key: str
    has_server_configured_llm: bool
    custom_llm_count: int
    issues: list[LLMConfigIssueResponse] = Field(default_factory=list)
    detail: str | None = None
    next_step: str | None = None


def _issue_response(issue: LLMConfigRegistrationIssue) -> LLMConfigIssueResponse:
    return LLMConfigIssueResponse(
        llm_key=issue.llm_key,
        missing_env_vars=list(issue.missing_env_vars),
        detail=issue.detail,
    )


def _server_llm_issue(llm_key: str) -> LLMConfigIssueResponse | None:
    if issue := LLMConfigRegistry.get_config_issue(llm_key):
        return _issue_response(issue)

    try:
        config = LLMConfigRegistry.get_config(llm_key)
    except InvalidLLMConfigError as exc:
        return LLMConfigIssueResponse(
            llm_key=llm_key,
            missing_env_vars=[],
            detail=str(exc),
        )

    missing_env_vars = config.get_missing_env_vars()
    if not missing_env_vars:
        return None

    return LLMConfigIssueResponse(
        llm_key=llm_key,
        missing_env_vars=missing_env_vars,
        detail=(
            f"{llm_key} is missing required environment variables: "
            f"{', '.join(missing_env_vars)}"
        ),
    )


async def _custom_llm_count() -> int:
    try:
        organization = await app.DATABASE.organizations.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
        if organization is None:
            return 0

        tokens = await app.DATABASE.organizations.get_valid_org_auth_tokens(
            organization_id=organization.organization_id,
            token_type=OrganizationAuthTokenType.custom_llm,
        )
    except Exception:
        LOG.warning("Unable to count local custom LLM configs", exc_info=True)
        return 0

    valid_count = 0
    for token in tokens:
        try:
            CustomLLMConfig.model_validate_json(token.token)
        except Exception:
            LOG.warning("Skipping invalid custom LLM token in diagnostics", token_id=token.id, exc_info=True)
            continue
        valid_count += 1
    return valid_count


async def evaluate_llm_status() -> LLMDiagnosticsResponse:
    default_issue = _server_llm_issue(settings.LLM_KEY)
    custom_llm_count = await _custom_llm_count()
    has_server_configured_llm = default_issue is None

    issues = [
        _issue_response(issue)
        for issue in LLMConfigRegistry.get_config_issues()
        if issue.llm_key != settings.LLM_KEY
    ]
    if default_issue:
        issues.insert(0, default_issue)

    if has_server_configured_llm or custom_llm_count > 0:
        return LLMDiagnosticsResponse(
            status=LLMDiagnosticsStatus.ok,
            default_llm_key=settings.LLM_KEY,
            has_server_configured_llm=has_server_configured_llm,
            custom_llm_count=custom_llm_count,
            issues=issues,
        )

    return LLMDiagnosticsResponse(
        status=LLMDiagnosticsStatus.setup_required,
        default_llm_key=settings.LLM_KEY,
        has_server_configured_llm=False,
        custom_llm_count=0,
        issues=issues,
        detail="Skyvern is running, but no usable LLM is configured for local OSS.",
        next_step="Open Settings > Custom LLMs and add an Ollama, OpenRouter, or OpenAI-compatible model.",
    )


@router.get("/status", response_model=LLMDiagnosticsResponse, include_in_schema=False)
async def llm_status(request: Request) -> LLMDiagnosticsResponse:
    _require_local_access(request)
    return await evaluate_llm_status()
