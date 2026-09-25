import structlog

from skyvern.schemas.runs import ProxyLocationInput
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest
from skyvern.utils.secret_headers import SECRET_HEADER_MASK, merge_masked_headers

LOG = structlog.get_logger()


def resolve_extra_http_headers(
    request: WorkflowCreateYAMLRequest, inherited_headers: dict[str, str] | None
) -> dict[str, str] | None:
    if "extra_http_headers" in request.model_fields_set:
        return request.extra_http_headers or {}
    return inherited_headers


def resolve_cdp_connect_headers(
    request: WorkflowCreateYAMLRequest, inherited_headers: dict[str, str] | None
) -> dict[str, str] | None:
    if "cdp_connect_headers" in request.model_fields_set:
        headers = request.cdp_connect_headers or {}
        if SECRET_HEADER_MASK in headers.values():
            LOG.debug("Merging masked copilot headers", field_name="cdp_connect_headers")
        return merge_masked_headers(headers, inherited_headers)
    return inherited_headers


def resolve_proxy_location(request: WorkflowCreateYAMLRequest, inherited: ProxyLocationInput) -> ProxyLocationInput:
    if "proxy_location" in request.model_fields_set:
        return request.proxy_location
    return inherited


def resolve_totp_identifier(request: WorkflowCreateYAMLRequest, inherited: str | None) -> str | None:
    if "totp_identifier" in request.model_fields_set:
        return request.totp_identifier
    return inherited


def resolve_totp_verification_url(request: WorkflowCreateYAMLRequest, inherited: str | None) -> str | None:
    if "totp_verification_url" in request.model_fields_set:
        return request.totp_verification_url
    return inherited


def resolve_webhook_callback_url(request: WorkflowCreateYAMLRequest, inherited: str | None) -> str | None:
    if "webhook_callback_url" in request.model_fields_set:
        return request.webhook_callback_url
    return inherited
