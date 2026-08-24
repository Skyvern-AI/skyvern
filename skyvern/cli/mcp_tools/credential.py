"""Skyvern MCP credential tools — CRUD for stored credentials.

Tools for listing, creating, and deleting credentials stored in Skyvern.
Credentials are used with skyvern_login to authenticate on websites without
exposing passwords in prompts. These tools do not require a browser session.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from skyvern.cli.core.client import has_api_key_override
from skyvern.client.core.api_error import ApiError
from skyvern.client.errors import NotFoundError
from skyvern.config import settings

from ._common import ErrorCode, Timer, make_error, make_result, raw_http_delete, raw_http_get, raw_http_post
from ._session import get_skyvern

_ONEPASSWORD_GET_PATH = "v1/credentials/onepassword/get"
_ONEPASSWORD_ITEMS_PATH = "v1/credentials/onepassword/items"
_ONEPASSWORD_CREATE_PATH = "v1/credentials/onepassword/create"
_ONEPASSWORD_CLEAR_PATH = "v1/credentials/onepassword"
_BITWARDEN_GET_PATH = "v1/credentials/bitwarden/get"
_BITWARDEN_ITEMS_PATH = "v1/credentials/bitwarden/items"
_BITWARDEN_CREATE_PATH = "v1/credentials/bitwarden/create"
_BITWARDEN_CLEAR_PATH = "v1/credentials/bitwarden"
_ONEPASSWORD_TOKEN_ENV = "OP_SERVICE_ACCOUNT_TOKEN"
_BITWARDEN_MASTER_PASSWORD_ENV = "BITWARDEN_MASTER_PASSWORD"
_REDACTED = "***redacted***"
_PROVIDER_CONFIG_SET_SINGLE_TENANT_ERROR = (
    "Provider configuration setters are only available in single-tenant (stdio/local) mode."
)
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "client_secret",
        "master_password",
        "password",
        "secret",
        "token",
    }
)
_BITWARDEN_CREDENTIAL_TYPES = frozenset({"password", "credit_card", "secret"})


def _not_found_error(tool: str, credential_id: str, timer: Timer) -> dict[str, Any]:
    return make_result(
        tool,
        ok=False,
        timing_ms=timer.timing_ms,
        error=make_error(
            ErrorCode.INVALID_INPUT,
            f"Credential not found: {credential_id}",
            "Use skyvern_credential_list to find valid credential IDs",
        ),
    )


def _validate_credential_id(credential_id: str, tool: str) -> dict[str, Any] | None:
    if "/" in credential_id or "\\" in credential_id:
        return make_result(
            tool,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "credential_id must not contain path separators",
                "Provide a valid credential ID (starts with cred_)",
            ),
        )
    if not credential_id.startswith("cred_"):
        return make_result(
            tool,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid credential_id format: {credential_id!r}",
                "Credential IDs start with cred_. Use skyvern_credential_list to find valid IDs.",
            ),
        )
    return None


def _serialize_credential(cred: Any) -> dict[str, Any]:
    """Pick the fields we expose from a CredentialResponse.

    Uses Any to avoid tight coupling with Fern-generated client types.
    Passwords and secrets are never returned — only metadata.
    """
    data: dict[str, Any] = {
        "credential_id": cred.credential_id,
        "name": cred.name,
        "credential_type": str(cred.credential_type),
    }

    # Serialize the credential metadata (no secrets)
    c = cred.credential
    if hasattr(c, "username"):
        data["username"] = c.username
        data["totp_type"] = str(c.totp_type) if hasattr(c, "totp_type") and c.totp_type else None
    elif hasattr(c, "last_four"):
        data["card_last_four"] = c.last_four
        data["card_brand"] = c.brand
    elif hasattr(c, "secret_label"):
        data["secret_label"] = c.secret_label

    return data


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, nested_value in value.items():
            if key in _SECRET_FIELD_NAMES and not isinstance(nested_value, (dict, list)):
                redacted[key] = None if nested_value is None else _REDACTED
            else:
                redacted[key] = _redact_secrets(nested_value)
        return redacted
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def _safe_config_payload(data: Any) -> dict[str, Any]:
    """Return provider config metadata while stripping secret material."""
    if not isinstance(data, dict):
        return {"configured": True, "response": _redact_secrets(data)}

    token = data.get("token")
    if isinstance(token, dict):
        safe_token = {key: _redact_secrets(value) for key, value in token.items() if key != "token"}
        return {"configured": True, "token": safe_token}

    return {"configured": True, "response": _redact_secrets(data)}


def _configured_false(provider: str) -> dict[str, Any]:
    return {"configured": False, "provider": provider}


def _missing_provider_secret(tool: str, provider: str, env_var: str) -> dict[str, Any]:
    return make_result(
        tool,
        ok=False,
        error=make_error(
            ErrorCode.INVALID_INPUT,
            f"{provider} secret is not configured in the MCP server environment",
            f"Set {env_var} before starting the MCP server, then call this tool again",
        ),
    )


def _request_scoped_provider_config_error(tool: str) -> dict[str, Any]:
    return make_result(
        tool,
        ok=False,
        error=make_error(
            ErrorCode.INVALID_INPUT,
            _PROVIDER_CONFIG_SET_SINGLE_TENANT_ERROR,
            "Use stdio/local MCP transport to configure provider credentials.",
        ),
    )


def _matches_search(item: dict[str, Any], search: str | None) -> bool:
    if not search:
        return True
    needle = search.casefold()
    searchable_fields = ("title", "url", "item_id", "vault_id", "vault_name", "collection_id", "credential_type")
    return any(needle in str(item.get(field, "")).casefold() for field in searchable_fields)


def _filter_provider_items(
    data: Any,
    *,
    tool: str,
    search: str | None,
    limit: int,
    exact_matches: dict[str, str | None],
) -> dict[str, Any]:
    if not isinstance(data, dict) or "configured" not in data or "items" not in data:
        return make_result(
            tool,
            ok=False,
            error=make_error(
                ErrorCode.API_ERROR,
                "Unexpected provider items response",
                "Check your Skyvern API version and connection",
            ),
        )

    items = data.get("items")
    if not isinstance(items, list):
        return make_result(
            tool,
            ok=False,
            error=make_error(
                ErrorCode.API_ERROR,
                "Provider items response did not include an items list",
                "Check your Skyvern API version and connection",
            ),
        )

    filtered_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not _matches_search(item, search):
            continue
        if any(expected is not None and str(item.get(field)) != expected for field, expected in exact_matches.items()):
            continue
        filtered_items.append(item)

    limited_items = filtered_items[:limit]
    return {
        "configured": bool(data["configured"]),
        "items": limited_items,
        "count": len(limited_items),
        "matched_count": len(filtered_items),
        "total_count": len(items),
        "has_more": len(filtered_items) > len(limited_items),
    }


async def _get_provider_config(tool: str, provider: str, path: str) -> dict[str, Any]:
    with Timer() as timer:
        try:
            data = await raw_http_get(path)
            timer.mark("http")
        except NotFoundError:
            return make_result(tool, data=_configured_false(provider), timing_ms=timer.timing_ms)
        except Exception as e:
            return make_result(
                tool,
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection"),
            )

    return make_result(tool, data=_safe_config_payload(data), timing_ms=timer.timing_ms)


async def _clear_provider_config(tool: str, provider: str, path: str) -> dict[str, Any]:
    with Timer() as timer:
        try:
            data = await raw_http_delete(path)
            timer.mark("http")
        except Exception as e:
            return make_result(
                tool,
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection"),
            )

    success = data.get("success", True) if isinstance(data, dict) else True
    return make_result(
        tool,
        data={"provider": provider, "cleared": bool(success)},
        timing_ms=timer.timing_ms,
    )


async def skyvern_credential_list(
    page: Annotated[int, Field(description="Page number (1-based)", ge=1)] = 1,
    page_size: Annotated[int, Field(description="Results per page", ge=1, le=100)] = 10,
) -> dict[str, Any]:
    """List stored credentials. Returns credential IDs and names — never passwords or secrets.

    Use this to find a credential_id for skyvern_login. Credentials are stored securely in Skyvern's vault.
    """
    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            credentials = await skyvern.get_credentials(page=page, page_size=page_size)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_credential_list",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection"),
            )

    return make_result(
        "skyvern_credential_list",
        data={
            "credentials": [_serialize_credential(c) for c in credentials],
            "page": page,
            "page_size": page_size,
            "count": len(credentials),
            "has_more": len(credentials) == page_size,
        },
        timing_ms=timer.timing_ms,
    )


# NOTE: Intentionally NOT registered as an MCP tool. Passwords must never flow through
# MCP tool calls. Credential creation happens via CLI (`skyvern credentials add`) or
# web UI. This function is preserved for programmatic SDK use only.
async def skyvern_credential_create(
    name: Annotated[str, Field(description="Human-readable name (e.g., 'Amazon Login', 'Salesforce Prod')")],
    credential_type: Annotated[
        str,
        Field(description="Type of credential: 'password', 'credit_card', or 'secret'"),
    ] = "password",
    username: Annotated[str | None, Field(description="Username or email (required for password type)")] = None,
    password: Annotated[
        str | None,
        Field(description="Password (optional for password type; omit or leave empty for logins without a password)"),
    ] = None,
    totp: Annotated[str | None, Field(description="TOTP secret for 2FA (e.g., 'JBSWY3DPEHPK3PXP')")] = None,
    card_number: Annotated[str | None, Field(description="Full card number (for credit_card type)")] = None,
    card_cvv: Annotated[str | None, Field(description="Card CVV (for credit_card type)")] = None,
    card_exp_month: Annotated[str | None, Field(description="Expiration month (for credit_card type)")] = None,
    card_exp_year: Annotated[str | None, Field(description="Expiration year (for credit_card type)")] = None,
    card_brand: Annotated[str | None, Field(description="Card brand, e.g. 'visa' (for credit_card type)")] = None,
    card_holder_name: Annotated[str | None, Field(description="Cardholder name (for credit_card type)")] = None,
    secret_value: Annotated[str | None, Field(description="Secret value (for secret type)")] = None,
    secret_label: Annotated[str | None, Field(description="Label for the secret (for secret type)")] = None,
) -> dict[str, Any]:
    """Store a credential securely in Skyvern's vault. Returns a credential_id for use with skyvern_login.

    The credential is encrypted and stored server-side. After creation, only metadata (username, card last 4) is returned — never the password or secret itself.
    """
    valid_types = ("password", "credit_card", "secret")
    if credential_type not in valid_types:
        return make_result(
            "skyvern_credential_create",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid credential_type: '{credential_type}'",
                f"Use one of: {', '.join(valid_types)}",
            ),
        )

    # Build credential payload per type
    credential_data: dict[str, Any]
    if credential_type == "password":
        if not username:
            return make_result(
                "skyvern_credential_create",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    "username is required for credential_type='password'",
                    "Provide a username; password may be empty for logins without a password",
                ),
            )
        credential_data = {"username": username, "password": password or ""}
        if totp:
            credential_data["totp"] = totp
    elif credential_type == "credit_card":
        cc_fields = {
            "card_number": card_number,
            "card_cvv": card_cvv,
            "card_exp_month": card_exp_month,
            "card_exp_year": card_exp_year,
            "card_brand": card_brand,
            "card_holder_name": card_holder_name,
        }
        missing = [k for k, v in cc_fields.items() if not v]
        if missing:
            return make_result(
                "skyvern_credential_create",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    f"Missing required fields for credit_card: {', '.join(missing)}",
                    f"Provide: {', '.join(missing)}",
                ),
            )
        credential_data = cc_fields  # type: ignore[assignment]
    else:
        if not secret_value:
            return make_result(
                "skyvern_credential_create",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    "secret_value is required for credential_type='secret'",
                    "Provide secret_value",
                ),
            )
        credential_data = {"secret_value": secret_value}
        if secret_label:
            credential_data["secret_label"] = secret_label

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            result = await skyvern.create_credential(
                name=name,
                credential_type=credential_type,  # type: ignore[arg-type]
                credential=credential_data,  # type: ignore[arg-type]
            )
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_credential_create",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and credential data"),
            )

    return make_result(
        "skyvern_credential_create",
        data=_serialize_credential(result),
        timing_ms=timer.timing_ms,
    )


async def skyvern_credential_get(
    credential_id: Annotated[str, Field(description="Credential ID (starts with cred_)")],
) -> dict[str, Any]:
    """Get a stored credential's metadata by ID. Returns name, type, and username — never the password or secret."""
    if err := _validate_credential_id(credential_id, "skyvern_credential_get"):
        return err

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            result = await skyvern.get_credential(credential_id)
            timer.mark("sdk")
        except ApiError as e:
            if e.status_code == 404:
                return _not_found_error("skyvern_credential_get", credential_id, timer)
            return make_result(
                "skyvern_credential_get",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection"),
            )
        except Exception as e:
            return make_result(
                "skyvern_credential_get",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection"),
            )

    return make_result(
        "skyvern_credential_get",
        data=_serialize_credential(result),
        timing_ms=timer.timing_ms,
    )


async def skyvern_credential_delete(
    credential_id: Annotated[str, Field(description="Credential ID to delete (starts with cred_)")],
) -> dict[str, Any]:
    """Permanently delete a stored credential. This cannot be undone."""
    if err := _validate_credential_id(credential_id, "skyvern_credential_delete"):
        return err

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            await skyvern.delete_credential(credential_id)
            timer.mark("sdk")
        except ApiError as e:
            if e.status_code == 404:
                return _not_found_error("skyvern_credential_delete", credential_id, timer)
            return make_result(
                "skyvern_credential_delete",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection"),
            )
        except Exception as e:
            return make_result(
                "skyvern_credential_delete",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection"),
            )

    return make_result(
        "skyvern_credential_delete",
        data={"credential_id": credential_id, "deleted": True},
        timing_ms=timer.timing_ms,
    )


async def skyvern_onepassword_items(
    search: Annotated[
        str | None,
        Field(description="Optional case-insensitive filter across title, URL, vault name, vault ID, and item ID"),
    ] = None,
    vault_id: Annotated[str | None, Field(description="Optional exact 1Password vault ID filter")] = None,
    limit: Annotated[int, Field(description="Maximum number of item metadata rows to return", ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """List 1Password item metadata for this organization. Returns vault/item IDs, never item field values."""
    with Timer() as timer:
        try:
            data = await raw_http_get(_ONEPASSWORD_ITEMS_PATH)
            timer.mark("http")
        except Exception as e:
            return make_result(
                "skyvern_onepassword_items",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your 1Password configuration"),
            )

    items_data = _filter_provider_items(
        data,
        tool="skyvern_onepassword_items",
        search=search,
        limit=limit,
        exact_matches={"vault_id": vault_id},
    )
    if items_data.get("ok") is False:
        return items_data
    return make_result("skyvern_onepassword_items", data=items_data, timing_ms=timer.timing_ms)


async def skyvern_bitwarden_items(
    search: Annotated[
        str | None,
        Field(description="Optional case-insensitive filter across title, URL, collection ID, item ID, and type"),
    ] = None,
    collection_id: Annotated[str | None, Field(description="Optional exact Bitwarden collection ID filter")] = None,
    credential_type: Annotated[
        str | None,
        Field(description="Optional exact credential type filter: 'password', 'credit_card', or 'secret'"),
    ] = None,
    limit: Annotated[int, Field(description="Maximum number of item metadata rows to return", ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """List Bitwarden item metadata for this organization. Returns collection/item IDs, never secret values."""
    if credential_type is not None and credential_type not in _BITWARDEN_CREDENTIAL_TYPES:
        return make_result(
            "skyvern_bitwarden_items",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid credential_type: {credential_type!r}",
                f"Use one of: {', '.join(sorted(_BITWARDEN_CREDENTIAL_TYPES))}",
            ),
        )

    with Timer() as timer:
        try:
            data = await raw_http_get(_BITWARDEN_ITEMS_PATH)
            timer.mark("http")
        except Exception as e:
            return make_result(
                "skyvern_bitwarden_items",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your Bitwarden configuration"),
            )

    items_data = _filter_provider_items(
        data,
        tool="skyvern_bitwarden_items",
        search=search,
        limit=limit,
        exact_matches={"collection_id": collection_id, "credential_type": credential_type},
    )
    if items_data.get("ok") is False:
        return items_data
    return make_result("skyvern_bitwarden_items", data=items_data, timing_ms=timer.timing_ms)


async def skyvern_onepassword_config_get() -> dict[str, Any]:
    """Get 1Password configuration metadata for this organization. The service account token is never returned."""
    return await _get_provider_config("skyvern_onepassword_config_get", "onepassword", _ONEPASSWORD_GET_PATH)


async def skyvern_onepassword_config_set() -> dict[str, Any]:
    """Store the 1Password token from OP_SERVICE_ACCOUNT_TOKEN. Set it before starting the MCP server."""
    if has_api_key_override():
        return _request_scoped_provider_config_error("skyvern_onepassword_config_set")

    token = settings.OP_SERVICE_ACCOUNT_TOKEN
    if not token:
        return _missing_provider_secret(
            "skyvern_onepassword_config_set",
            "1Password",
            _ONEPASSWORD_TOKEN_ENV,
        )

    with Timer() as timer:
        try:
            data = await raw_http_post(_ONEPASSWORD_CREATE_PATH, json_body={"token": token})
            timer.mark("http")
        except Exception:
            return make_result(
                "skyvern_onepassword_config_set",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.API_ERROR,
                    "Failed to set 1Password configuration",
                    f"Check {_ONEPASSWORD_TOKEN_ENV} in the MCP server environment and the Skyvern connection",
                ),
            )

    return make_result("skyvern_onepassword_config_set", data=_safe_config_payload(data), timing_ms=timer.timing_ms)


async def skyvern_onepassword_config_clear() -> dict[str, Any]:
    """Clear the 1Password service account token for this organization."""
    return await _clear_provider_config("skyvern_onepassword_config_clear", "onepassword", _ONEPASSWORD_CLEAR_PATH)


async def skyvern_bitwarden_config_get() -> dict[str, Any]:
    """Get Bitwarden configuration metadata for this organization. The master password is never returned."""
    return await _get_provider_config("skyvern_bitwarden_config_get", "bitwarden", _BITWARDEN_GET_PATH)


async def skyvern_bitwarden_config_set(
    email: Annotated[str, Field(description="Bitwarden account email")],
) -> dict[str, Any]:
    """Store the Bitwarden credential using BITWARDEN_MASTER_PASSWORD from the MCP server environment."""
    if has_api_key_override():
        return _request_scoped_provider_config_error("skyvern_bitwarden_config_set")

    master_password = settings.BITWARDEN_MASTER_PASSWORD
    if not master_password:
        return _missing_provider_secret(
            "skyvern_bitwarden_config_set",
            "Bitwarden",
            _BITWARDEN_MASTER_PASSWORD_ENV,
        )

    with Timer() as timer:
        try:
            data = await raw_http_post(
                _BITWARDEN_CREATE_PATH,
                json_body={"credential": {"email": email, "master_password": master_password}},
            )
            timer.mark("http")
        except Exception:
            return make_result(
                "skyvern_bitwarden_config_set",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(
                    ErrorCode.API_ERROR,
                    "Failed to set Bitwarden configuration",
                    f"Check {_BITWARDEN_MASTER_PASSWORD_ENV} in the MCP server environment and the Skyvern connection",
                ),
            )

    return make_result("skyvern_bitwarden_config_set", data=_safe_config_payload(data), timing_ms=timer.timing_ms)


async def skyvern_bitwarden_config_clear() -> dict[str, Any]:
    """Clear the Bitwarden credential for this organization."""
    return await _clear_provider_config("skyvern_bitwarden_config_clear", "bitwarden", _BITWARDEN_CLEAR_PATH)
