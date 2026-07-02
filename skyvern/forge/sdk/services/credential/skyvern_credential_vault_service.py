import json
import os
import re
import secrets
from contextlib import suppress
from pathlib import Path

import structlog
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.schemas.credentials import (
    CreateCredentialRequest,
    Credential,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    CreditCardCredential,
)
from skyvern.forge.sdk.services.credential.credential_vault_service import CredentialVaultService

LOG = structlog.get_logger()

_ITEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class SkyvernCredentialVaultService(CredentialVaultService):
    """Local encrypted credential vault for self-hosted Skyvern deployments."""

    def __init__(self) -> None:
        self._fernet: Fernet | None = None

    async def create_credential(self, organization_id: str, data: CreateCredentialRequest) -> Credential:
        item_id = self._generate_item_id()
        item = CredentialItem(
            item_id=item_id,
            name=data.name,
            credential_type=data.credential_type,
            credential=data.credential,
        )
        await self._store_item(item)

        try:
            return await self._create_db_credential(
                organization_id=organization_id,
                data=data,
                item_id=item_id,
                vault_type=CredentialVaultType.SKYVERN,
            )
        except Exception:
            self._delete_item_file(item_id)
            raise

    async def update_credential(self, credential: Credential, data: CreateCredentialRequest) -> Credential:
        credential_data = data.credential
        if data.credential_type == CredentialType.CREDIT_CARD and isinstance(credential_data, CreditCardCredential):
            credential_data = await self._preserve_omitted_credit_card_fields(
                credential=credential,
                updated_credential=credential_data,
            )

        new_item_id = self._generate_item_id()
        item = CredentialItem(
            item_id=new_item_id,
            name=data.name,
            credential_type=data.credential_type,
            credential=credential_data,
        )
        await self._store_item(item)

        try:
            return await self._update_db_credential(
                credential=credential,
                data=data,
                item_id=new_item_id,
            )
        except Exception:
            self._delete_item_file(new_item_id)
            raise

    async def delete_credential(self, credential: Credential) -> None:
        await app.DATABASE.credentials.delete_credential(credential.credential_id, credential.organization_id)
        self._delete_item_file(credential.item_id)

    async def post_delete_credential_item(self, item_id: str, organization_id: str | None = None) -> None:
        self._delete_item_file(item_id)

    async def get_credential_item(self, db_credential: Credential) -> CredentialItem:
        item_path = self._item_path(db_credential.item_id)
        if not item_path.exists():
            raise HTTPException(status_code=404, detail="Credential vault item not found")

        try:
            encrypted_payload = item_path.read_bytes()
            payload = self._get_fernet().decrypt(encrypted_payload).decode("utf-8")
            item = CredentialItem.model_validate(json.loads(payload))
        except InvalidToken as exc:
            raise HTTPException(status_code=500, detail="Credential vault item could not be decrypted") from exc
        except Exception as exc:
            LOG.warning(
                "Failed to read local credential vault item",
                credential_id=db_credential.credential_id,
                item_id=db_credential.item_id,
                error_type=type(exc).__name__,
            )
            raise HTTPException(status_code=500, detail="Credential vault item could not be read") from exc

        return item

    async def _store_item(self, item: CredentialItem) -> None:
        item_path = self._item_path(item.item_id)
        payload = json.dumps(item.model_dump(mode="json"), separators=(",", ":"))
        encrypted_payload = self._get_fernet().encrypt(payload.encode("utf-8"))

        temp_path = item_path.with_suffix(f".{secrets.token_urlsafe(8)}.tmp")
        try:
            self._write_file_durable(temp_path, encrypted_payload, mode=0o600)
            temp_path.replace(item_path)
            self._fsync_directory_best_effort(item_path.parent)
        finally:
            with suppress(FileNotFoundError):
                temp_path.unlink()

    def _get_fernet(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet

        key = settings.LOCAL_CREDENTIAL_VAULT_KEY
        if key:
            self._fernet = Fernet(key.encode("utf-8"))
            return self._fernet

        vault_dir = self._vault_dir()
        key_path = vault_dir / ".fernet_key"
        if key_path.exists():
            key_bytes = key_path.read_bytes().strip()
        else:
            key_bytes = self._create_or_read_key_file(key_path)

        self._fernet = Fernet(key_bytes)
        return self._fernet

    def _create_or_read_key_file(self, key_path: Path) -> bytes:
        key_bytes = Fernet.generate_key()
        temp_path = key_path.with_name(f".fernet_key.{secrets.token_urlsafe(8)}.tmp")
        try:
            self._write_file_durable(temp_path, key_bytes + b"\n", mode=0o600)
            try:
                os.link(temp_path, key_path)
                self._fsync_directory_best_effort(key_path.parent)
                return key_bytes
            except FileExistsError:
                return key_path.read_bytes().strip()
        finally:
            with suppress(FileNotFoundError):
                temp_path.unlink()

    def _vault_dir(self) -> Path:
        vault_dir = Path(settings.LOCAL_CREDENTIAL_VAULT_PATH).expanduser().resolve()
        vault_dir.mkdir(parents=True, exist_ok=True)
        self._chmod_best_effort(vault_dir, 0o700)
        return vault_dir

    def _item_path(self, item_id: str) -> Path:
        if not item_id or not _ITEM_ID_PATTERN.fullmatch(item_id):
            raise HTTPException(status_code=400, detail="Invalid credential vault item ID")
        return self._vault_dir() / f"{item_id}.bin"

    def _delete_item_file(self, item_id: str) -> None:
        try:
            self._item_path(item_id).unlink(missing_ok=True)
        except HTTPException:
            raise
        except Exception:
            LOG.warning("Failed to delete local credential vault item", item_id=item_id, exc_info=True)

    @staticmethod
    def _generate_item_id() -> str:
        return f"creditem_{secrets.token_urlsafe(24)}"

    @staticmethod
    def _chmod_best_effort(path: Path, mode: int) -> None:
        with suppress(OSError):
            os.chmod(path, mode)

    @staticmethod
    def _write_file_durable(path: Path, payload: bytes, *, mode: int) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(fd, "wb") as file:
                fd = -1
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
        finally:
            if fd != -1:
                os.close(fd)

    @staticmethod
    def _fsync_directory_best_effort(path: Path) -> None:
        with suppress(OSError):
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
