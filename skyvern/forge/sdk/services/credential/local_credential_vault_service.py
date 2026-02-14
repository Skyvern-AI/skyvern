import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Union

import aiofiles
import structlog
from pydantic import BaseModel, Field, TypeAdapter

from skyvern.config import settings
from skyvern.constants import REPO_ROOT_DIR
from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    CreditCardCredential,
    PasswordCredential,
    SecretCredential,
)
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService

LOG = structlog.get_logger()


class LocalCredentialVaultService(CredentialVaultService):
    """Local file-based credential vault service for dev/testing/hobby self-hosting.

    WARNING: Credentials are stored unencrypted. Do not use in production with sensitive data.
    """

    class _PasswordCredentialData(BaseModel):
        type: Literal["password"]
        username: str
        password: str
        totp: str | None = None

    class _CreditCardCredentialData(BaseModel):
        type: Literal["credit_card"]
        card_number: str
        card_cvv: str
        card_exp_month: str
        card_exp_year: str
        card_brand: str
        card_holder_name: str

    class _SecretCredentialData(BaseModel):
        type: Literal["secret"]
        secret_value: str
        secret_label: str | None = None

    _CredentialData = Annotated[
        Union[_PasswordCredentialData, _CreditCardCredentialData, _SecretCredentialData],
        Field(discriminator="type"),
    ]

    class _StoredCredential(BaseModel):
        organization_id: str
        name: str
        type: str
        created_at: str
        data: dict

    class _CredentialStore(BaseModel):
        version: str = "1.0"
        credentials: dict[str, "LocalCredentialVaultService._StoredCredential"] = {}

    def __init__(self, file_path: str | None = None):
        """Initialize the local credential vault service.

        Args:
            file_path: Path to the credentials JSON file. If None, uses settings.LOCAL_CREDENTIAL_FILE.
        """
        self._file_path = self._resolve_file_path(file_path)
        self._lock = asyncio.Lock()
        LOG.info("LocalCredentialVaultService initialized", file_path=str(self._file_path))

    def _resolve_file_path(self, file_path: str | None) -> Path:
        """Resolve the credential file path."""
        if file_path:
            path = Path(file_path)
        else:
            path = Path(settings.LOCAL_CREDENTIAL_FILE)

        if not path.is_absolute():
            path = Path(REPO_ROOT_DIR) / path

        return path.resolve()

    async def _read_store(self) -> _CredentialStore:
        """Read the credential store from file."""
        if not self._file_path.exists():
            return self._CredentialStore()

        try:
            async with aiofiles.open(self._file_path, "r", encoding="utf-8") as f:
                content = await f.read()
                if not content.strip():
                    return self._CredentialStore()
                data = json.loads(content)
                return self._CredentialStore.model_validate(data)
        except json.JSONDecodeError as e:
            LOG.error("Failed to parse local credential file", error=str(e), file_path=str(self._file_path))
            raise ValueError(f"Invalid JSON in credential file: {e}") from e
        except Exception as e:
            LOG.error("Failed to read local credential file", error=str(e), file_path=str(self._file_path))
            raise ValueError(f"Failed to read credential file: {e}") from e

    async def _write_store(self, store: _CredentialStore) -> None:
        """Write the credential store to file."""
        try:
            # Ensure parent directory exists
            self._file_path.parent.mkdir(parents=True, exist_ok=True)

            # Write to temp file first, then rename (atomic on POSIX)
            temp_path = self._file_path.with_suffix(".tmp")
            content = store.model_dump_json(indent=2)

            async with aiofiles.open(temp_path, "w", encoding="utf-8") as f:
                await f.write(content)

            # Atomic rename
            temp_path.replace(self._file_path)

        except Exception as e:
            LOG.error("Failed to write local credential file", error=str(e), file_path=str(self._file_path))
            raise ValueError(f"Failed to write credential file: {e}") from e

    def _generate_item_id(self, organization_id: str) -> str:
        """Generate a unique item ID for a credential."""
        return f"{organization_id}-{uuid.uuid4()}".replace("_", "")

    def _serialize_credential_data(
        self, credential: PasswordCredential | CreditCardCredential | SecretCredential
    ) -> dict:
        """Serialize credential data to a dictionary."""
        if isinstance(credential, PasswordCredential):
            return self._PasswordCredentialData(
                type="password",
                username=credential.username,
                password=credential.password,
                totp=credential.totp,
            ).model_dump()
        elif isinstance(credential, CreditCardCredential):
            return self._CreditCardCredentialData(
                type="credit_card",
                card_number=credential.card_number,
                card_cvv=credential.card_cvv,
                card_exp_month=credential.card_exp_month,
                card_exp_year=credential.card_exp_year,
                card_brand=credential.card_brand,
                card_holder_name=credential.card_holder_name,
            ).model_dump()
        elif isinstance(credential, SecretCredential):
            return self._SecretCredentialData(
                type="secret",
                secret_value=credential.secret_value,
                secret_label=credential.secret_label,
            ).model_dump()
        else:
            raise TypeError(f"Unsupported credential type: {type(credential)}")

    async def create_credential(self, organization_id: str, data: CreateCredentialRequest) -> Credential:
        """Create a new credential in the local file and database."""
        LOG.info(
            "Creating credential in local storage",
            organization_id=organization_id,
            name=data.name,
            credential_type=data.credential_type,
        )

        item_id = self._generate_item_id(organization_id)

        async with self._lock:
            store = await self._read_store()

            # Store credential data
            stored_credential = self._StoredCredential(
                organization_id=organization_id,
                name=data.name,
                type=data.credential_type.value,
                created_at=datetime.now(timezone.utc).isoformat(),
                data=self._serialize_credential_data(data.credential),
            )
            store.credentials[item_id] = stored_credential

            await self._write_store(store)

        # Create database record
        try:
            credential = await self._create_db_credential(
                organization_id=organization_id,
                data=data,
                item_id=item_id,
                vault_type=CredentialVaultType.LOCAL,
            )
        except Exception:
            # Rollback: remove from local storage
            LOG.warning(
                "DB creation failed, rolling back local storage",
                organization_id=organization_id,
                item_id=item_id,
            )
            async with self._lock:
                store = await self._read_store()
                store.credentials.pop(item_id, None)
                await self._write_store(store)
            raise

        LOG.info(
            "Successfully created credential in local storage",
            organization_id=organization_id,
            credential_id=credential.credential_id,
            item_id=item_id,
        )
        return credential

    async def delete_credential(self, credential: Credential) -> None:
        """Delete a credential from the local file and database."""
        LOG.info(
            "Deleting credential from local storage",
            organization_id=credential.organization_id,
            credential_id=credential.credential_id,
            item_id=credential.item_id,
        )

        # Delete from database first
        await app.DATABASE.delete_credential(credential.credential_id, credential.organization_id)

        # Delete from local storage
        async with self._lock:
            store = await self._read_store()
            if credential.item_id in store.credentials:
                del store.credentials[credential.item_id]
                await self._write_store(store)

        LOG.info(
            "Successfully deleted credential from local storage",
            organization_id=credential.organization_id,
            credential_id=credential.credential_id,
        )

    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        """Retrieve the full credential data from the local file."""
        LOG.debug(
            "Retrieving credential from local storage",
            credential_id=db_credential.credential_id,
            item_id=db_credential.item_id,
        )

        async with self._lock:
            store = await self._read_store()

        if db_credential.item_id not in store.credentials:
            raise ValueError(f"Credential not found in local storage: {db_credential.item_id}")

        stored = store.credentials[db_credential.item_id]

        # Validate organization ownership
        if stored.organization_id != db_credential.organization_id:
            LOG.warning(
                "Organization mismatch when retrieving credential",
                expected_org=db_credential.organization_id,
                stored_org=stored.organization_id,
                item_id=db_credential.item_id,
            )
            raise ValueError(f"Credential not found for organization: {db_credential.item_id}")

        # Deserialize credential data using discriminated union
        data = TypeAdapter(LocalCredentialVaultService._CredentialData).validate_python(stored.data)

        if isinstance(data, LocalCredentialVaultService._PasswordCredentialData):
            return CredentialItem(
                item_id=db_credential.item_id,
                name=db_credential.name,
                credential_type=CredentialType.PASSWORD,
                credential=PasswordCredential(
                    username=data.username,
                    password=data.password,
                    totp=data.totp,
                    totp_type=db_credential.totp_type,
                ),
            )
        elif isinstance(data, LocalCredentialVaultService._CreditCardCredentialData):
            return CredentialItem(
                item_id=db_credential.item_id,
                name=db_credential.name,
                credential_type=CredentialType.CREDIT_CARD,
                credential=CreditCardCredential(
                    card_number=data.card_number,
                    card_cvv=data.card_cvv,
                    card_exp_month=data.card_exp_month,
                    card_exp_year=data.card_exp_year,
                    card_brand=data.card_brand,
                    card_holder_name=data.card_holder_name,
                ),
            )
        elif isinstance(data, LocalCredentialVaultService._SecretCredentialData):
            return CredentialItem(
                item_id=db_credential.item_id,
                name=db_credential.name,
                credential_type=CredentialType.SECRET,
                credential=SecretCredential(
                    secret_value=data.secret_value,
                    secret_label=data.secret_label,
                ),
            )
        else:
            raise TypeError(f"Unknown credential data type: {type(data)}")
