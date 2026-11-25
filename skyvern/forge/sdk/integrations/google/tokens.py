import datetime
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.exc import SQLAlchemyError

from skyvern.forge import app
from skyvern.forge.sdk.db.repositories.google_oauth import STATE_ACTIVE
from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase
from skyvern.forge.sdk.services import google_oauth_service


class DrivePickerTokenStatus(StrEnum):
    READY = "ready"
    OWNER_ONLY = "owner_only"
    PICKER_RECONNECT_REQUIRED = "picker_reconnect_required"
    TOKEN_TOO_BROAD = "token_too_broad"
    RECONNECT_REQUIRED = "reconnect_required"
    RETRY_REQUIRED = "retry_required"
    DATABASE_UNAVAILABLE = "database_unavailable"
    CONFIGURATION_UNAVAILABLE = "configuration_unavailable"


@dataclass(frozen=True)
class DrivePickerToken:
    status: DrivePickerTokenStatus
    access_token: str | None = field(default=None, repr=False)
    expires_at: datetime.datetime | None = None


async def _owned_credential(
    organization_id: str,
    credential_id: str,
    current_user_id: str | None,
) -> tuple[DrivePickerTokenStatus | None, GoogleOAuthCredentialBase | None]:
    try:
        credential = await app.DATABASE.google_oauth.get_credential(organization_id, credential_id)
    except SQLAlchemyError:
        return DrivePickerTokenStatus.DATABASE_UNAVAILABLE, None
    if credential is None or current_user_id is None or credential.initiating_user_id != current_user_id:
        return DrivePickerTokenStatus.OWNER_ONLY, None
    return None, credential


async def resolve_drive_picker_token(
    organization_id: str,
    credential_id: str,
    current_user_id: str | None,
) -> DrivePickerToken:
    status, credential = await _owned_credential(organization_id, credential_id, current_user_id)
    if status is not None:
        return DrivePickerToken(status)
    if credential is None:
        return DrivePickerToken(DrivePickerTokenStatus.OWNER_ONLY)
    if credential.state != STATE_ACTIVE:
        return DrivePickerToken(DrivePickerTokenStatus.RECONNECT_REQUIRED)
    if google_oauth_service.GOOGLE_DRIVE_FILE_SCOPE not in credential.scopes_granted:
        return DrivePickerToken(DrivePickerTokenStatus.PICKER_RECONNECT_REQUIRED)

    try:
        credential_secrets = await google_oauth_service.load_credential_secrets(organization_id, credential_id)
    except ValueError:
        return DrivePickerToken(DrivePickerTokenStatus.RECONNECT_REQUIRED)
    except SQLAlchemyError:
        return DrivePickerToken(DrivePickerTokenStatus.DATABASE_UNAVAILABLE)

    if credential_secrets.credential_version != credential.modified_at:
        return DrivePickerToken(DrivePickerTokenStatus.RETRY_REQUIRED)

    try:
        refreshed = await google_oauth_service.refresh_and_rotate(
            organization_id=organization_id,
            credential_id=credential_id,
            credential_secrets=credential_secrets,
            requested_scopes=(google_oauth_service.GOOGLE_DRIVE_FILE_SCOPE,),
        )
    except google_oauth_service.InvalidScopeError:
        return DrivePickerToken(DrivePickerTokenStatus.PICKER_RECONNECT_REQUIRED)
    except google_oauth_service.ExpiredRefreshTokenError:
        try:
            await google_oauth_service.mark_credential_expired(
                organization_id,
                credential_id,
                expected_version=credential_secrets.credential_version,
            )
        except SQLAlchemyError:
            return DrivePickerToken(DrivePickerTokenStatus.DATABASE_UNAVAILABLE)
        return DrivePickerToken(DrivePickerTokenStatus.RECONNECT_REQUIRED)
    except (
        google_oauth_service.ClientConfigMismatchError,
        google_oauth_service.MissingAccessTokenError,
        google_oauth_service.RotatedRefreshTokenPersistenceError,
    ):
        return DrivePickerToken(DrivePickerTokenStatus.RECONNECT_REQUIRED)
    except google_oauth_service.OrganizationClientConfigUnavailableError:
        return DrivePickerToken(DrivePickerTokenStatus.CONFIGURATION_UNAVAILABLE)

    if not google_oauth_service.is_narrow_drive_grant(refreshed.granted_scopes):
        return DrivePickerToken(DrivePickerTokenStatus.TOKEN_TOO_BROAD)
    status, current_credential = await _owned_credential(organization_id, credential_id, current_user_id)
    if status is not None:
        return DrivePickerToken(status)
    if current_credential is None:
        return DrivePickerToken(DrivePickerTokenStatus.OWNER_ONLY)
    if current_credential.state != STATE_ACTIVE:
        return DrivePickerToken(DrivePickerTokenStatus.RECONNECT_REQUIRED)
    if current_credential.modified_at != refreshed.credential_version:
        return DrivePickerToken(DrivePickerTokenStatus.RETRY_REQUIRED)
    return DrivePickerToken(
        DrivePickerTokenStatus.READY,
        access_token=refreshed.access_token,
        expires_at=refreshed.expires_at,
    )
