from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db._sentinels import _UNSET
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import CredentialModel, OrganizationBitwardenCollectionModel
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType, CredentialVaultType
from skyvern.forge.sdk.schemas.organization_bitwarden_collections import OrganizationBitwardenCollection


class CredentialRepository(BaseRepository):
    """Database operations for credential and Bitwarden collection management."""

    @db_operation("create_credential")
    async def create_credential(
        self,
        organization_id: str,
        name: str,
        vault_type: CredentialVaultType,
        item_id: str,
        credential_type: CredentialType,
        username: str | None,
        totp_type: str,
        card_last4: str | None,
        card_brand: str | None,
        totp_identifier: str | None = None,
        secret_label: str | None = None,
    ) -> Credential:
        async with self.Session() as session:
            credential = CredentialModel(
                organization_id=organization_id,
                name=name,
                vault_type=vault_type,
                item_id=item_id,
                credential_type=credential_type,
                username=username,
                totp_type=totp_type,
                totp_identifier=totp_identifier,
                card_last4=card_last4,
                card_brand=card_brand,
                secret_label=secret_label,
            )
            session.add(credential)
            await session.commit()
            await session.refresh(credential)
            return Credential.model_validate(credential)

    @db_operation("get_credential")
    async def get_credential(self, credential_id: str, organization_id: str) -> Credential | None:
        async with self.Session() as session:
            credential = (
                await session.scalars(
                    select(CredentialModel)
                    .filter_by(credential_id=credential_id)
                    .filter_by(organization_id=organization_id)
                    .filter(CredentialModel.deleted_at.is_(None))
                )
            ).first()
            if credential:
                return Credential.model_validate(credential)
            return None

    @db_operation("get_credentials")
    async def get_credentials(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        vault_type: str | None = None,
    ) -> list[Credential]:
        async with self.Session() as session:
            query = (
                select(CredentialModel)
                .filter_by(organization_id=organization_id)
                .filter(CredentialModel.deleted_at.is_(None))
            )
            if vault_type is not None:
                query = query.filter(CredentialModel.vault_type == vault_type)
            credentials = (
                await session.scalars(
                    query.order_by(CredentialModel.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
                )
            ).all()
            return [Credential.model_validate(credential) for credential in credentials]

    @db_operation("update_credential")
    async def update_credential(
        self,
        credential_id: str,
        organization_id: str,
        name: str | None = None,
        browser_profile_id: str | None | object = _UNSET,
        tested_url: str | None | object = _UNSET,
    ) -> Credential:
        async with self.Session() as session:
            credential = (
                await session.scalars(
                    select(CredentialModel)
                    .filter_by(credential_id=credential_id)
                    .filter_by(organization_id=organization_id)
                    .filter(CredentialModel.deleted_at.is_(None))
                )
            ).first()
            if not credential:
                raise NotFoundError(f"Credential {credential_id} not found")
            if name is not None:
                credential.name = name
            if browser_profile_id is not _UNSET:
                credential.browser_profile_id = browser_profile_id
            if tested_url is not _UNSET:
                credential.tested_url = tested_url
            await session.commit()
            await session.refresh(credential)
            return Credential.model_validate(credential)

    @db_operation("update_credential_vault_data")
    async def update_credential_vault_data(
        self,
        credential_id: str,
        organization_id: str,
        item_id: str,
        name: str,
        credential_type: CredentialType,
        username: str | None = None,
        totp_type: str = "none",
        totp_identifier: str | None = None,
        card_last4: str | None = None,
        card_brand: str | None = None,
        secret_label: str | None = None,
    ) -> Credential:
        async with self.Session() as session:
            credential = (
                await session.scalars(
                    select(CredentialModel)
                    .filter_by(credential_id=credential_id)
                    .filter_by(organization_id=organization_id)
                    .filter(CredentialModel.deleted_at.is_(None))
                    .with_for_update()
                )
            ).first()
            if not credential:
                raise NotFoundError(f"Credential {credential_id} not found")
            credential.item_id = item_id
            credential.name = name
            credential.credential_type = credential_type
            credential.username = username
            credential.totp_type = totp_type
            credential.totp_identifier = totp_identifier
            credential.card_last4 = card_last4
            credential.card_brand = card_brand
            credential.secret_label = secret_label
            await session.commit()
            await session.refresh(credential)
            return Credential.model_validate(credential)

    @db_operation("delete_credential")
    async def delete_credential(self, credential_id: str, organization_id: str) -> None:
        async with self.Session() as session:
            credential = (
                await session.scalars(
                    select(CredentialModel)
                    .filter_by(credential_id=credential_id)
                    .filter_by(organization_id=organization_id)
                )
            ).first()
            if not credential:
                raise NotFoundError(f"Credential {credential_id} not found")
            credential.deleted_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(credential)
            return None

    @db_operation("create_organization_bitwarden_collection")
    async def create_organization_bitwarden_collection(
        self,
        organization_id: str,
        collection_id: str,
    ) -> OrganizationBitwardenCollection:
        async with self.Session() as session:
            organization_bitwarden_collection = OrganizationBitwardenCollectionModel(
                organization_id=organization_id, collection_id=collection_id
            )
            session.add(organization_bitwarden_collection)
            await session.commit()
            await session.refresh(organization_bitwarden_collection)
            return OrganizationBitwardenCollection.model_validate(organization_bitwarden_collection)

    @db_operation("get_organization_bitwarden_collection")
    async def get_organization_bitwarden_collection(
        self,
        organization_id: str,
    ) -> OrganizationBitwardenCollection | None:
        async with self.Session() as session:
            organization_bitwarden_collection = (
                await session.scalars(
                    select(OrganizationBitwardenCollectionModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(deleted_at=None)
                )
            ).first()
            if organization_bitwarden_collection:
                return OrganizationBitwardenCollection.model_validate(organization_bitwarden_collection)
            return None
