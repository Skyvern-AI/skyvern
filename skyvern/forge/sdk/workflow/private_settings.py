from collections.abc import Mapping
from typing import Any

import structlog
from pydantic import ValidationError

from skyvern.schemas.proxy_location import GeoTarget
from skyvern.schemas.runs import ProxyLocationInput
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest
from skyvern.utils.secret_headers import SECRET_HEADER_MASK, merge_masked_headers

LOG = structlog.get_logger()

PRIVATE_WORKFLOW_SETTINGS_FIELDS = (
    "extra_http_headers",
    "cdp_connect_headers",
    "proxy_location",
    "totp_identifier",
    "totp_verification_url",
    "webhook_callback_url",
)


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
            LOG.debug("Merging masked headers", field_name="cdp_connect_headers")
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


def _is_private_proxy_mapping(proxy_location: object) -> bool:
    if not isinstance(proxy_location, Mapping):
        return False
    unknown_keys = set(proxy_location) - set(GeoTarget.model_fields)
    if unknown_keys:
        return True
    try:
        GeoTarget.model_validate(proxy_location)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        LOG.warning(
            "Withholding invalid geo proxy mapping from authoring YAML",
            field_names=sorted({str(error["loc"][0]) for error in errors if error["loc"]}),
            error_types=sorted({error["type"] for error in errors}),
        )
        return True
    return False


def strip_private_workflow_settings(data: dict[str, Any]) -> bool:
    changed = False
    for key in ("extra_http_headers", "cdp_connect_headers"):
        if key not in data:
            continue
        headers = data[key]
        if (
            headers
            and isinstance(headers, dict)
            and all(isinstance(name, str) and isinstance(value, str) for name, value in headers.items())
        ):
            masked = dict.fromkeys(headers, SECRET_HEADER_MASK)
            changed |= headers != masked
            data[key] = masked
        else:
            del data[key]
            changed = True
    for key in (
        "totp_identifier",
        "totp_verification_url",
        "webhook_callback_url",
    ):
        if key in data:
            del data[key]
            changed = True
    if _is_private_proxy_mapping(data.get("proxy_location")):
        del data["proxy_location"]
        changed = True
    return changed
