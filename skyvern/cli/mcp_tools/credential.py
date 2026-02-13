"""Skyvern MCP credential tools — CRUD for stored credentials.

Tools for listing, creating, and deleting credentials stored in Skyvern.
Credentials are used with skyvern_login to authenticate on websites without
exposing passwords in prompts. These tools do not require a browser session.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from skyvern.client.core.api_error import ApiError

from ._common import ErrorCode, Timer, make_error, make_result
from ._session import get_skyvern


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
    password: Annotated[str | None, Field(description="Password (required for password type)")] = None,
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
        if not username or not password:
            return make_result(
                "skyvern_credential_create",
                ok=False,
                error=make_error(
                    ErrorCode.INVALID_INPUT,
                    "username and password are required for credential_type='password'",
                    "Provide both username and password",
                ),
            )
        credential_data = {"username": username, "password": password}
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
