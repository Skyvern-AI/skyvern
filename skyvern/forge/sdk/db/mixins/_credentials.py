from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import (
    AzureVaultCredentialParameterModel,
    Base,
    BitwardenCreditCardDataParameterModel,
    BitwardenLoginCredentialParameterModel,
    BitwardenSensitiveInformationParameterModel,
    CredentialModel,
    CredentialParameterModel,
    OnePasswordCredentialParameterModel,
    OrganizationBitwardenCollectionModel,
)
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType, CredentialVaultType
from skyvern.forge.sdk.schemas.organization_bitwarden_collections import OrganizationBitwardenCollection
from skyvern.forge.sdk.workflow.models.parameter import (
    AzureVaultCredentialParameter,
    BitwardenCreditCardDataParameter,
    BitwardenLoginCredentialParameter,
    BitwardenSensitiveInformationParameter,
    CredentialParameter,
    OnePasswordCredentialParameter,
)

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB

LOG = structlog.get_logger()

_UNSET = object()


class CredentialsMixin:
    """Mixin providing credential database operations.

    Requires: self.Session (from BaseAlchemyDB)
    """

    async def create_credential(
        self: BaseAlchemyDB,
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

    async def get_credential(self: BaseAlchemyDB, credential_id: str, organization_id: str) -> Credential | None:
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

    async def get_credentials(
        self: BaseAlchemyDB, organization_id: str, page: int = 1, page_size: int = 10
    ) -> list[Credential]:
        async with self.Session() as session:
            credentials = (
                await session.scalars(
                    select(CredentialModel)
                    .filter_by(organization_id=organization_id)
                    .filter(CredentialModel.deleted_at.is_(None))
                    .order_by(CredentialModel.created_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
            return [Credential.model_validate(credential) for credential in credentials]

    async def update_credential(
        self: BaseAlchemyDB,
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

    async def update_credential_vault_data(
        self: BaseAlchemyDB,
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

    async def delete_credential(self: BaseAlchemyDB, credential_id: str, organization_id: str) -> None:
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
            credential.deleted_at = datetime.utcnow()
            await session.commit()
            await session.refresh(credential)
            return None

    async def create_organization_bitwarden_collection(
        self: BaseAlchemyDB,
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

    async def get_organization_bitwarden_collection(
        self: BaseAlchemyDB,
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

    @staticmethod
    def _convert_credential_parameter_to_model(
        parameter: (
            BitwardenLoginCredentialParameter
            | BitwardenSensitiveInformationParameter
            | BitwardenCreditCardDataParameter
            | CredentialParameter
            | OnePasswordCredentialParameter
            | AzureVaultCredentialParameter
        ),
    ) -> Base:
        """Convert a credential parameter object to its corresponding SQLAlchemy model."""
        if isinstance(parameter, BitwardenLoginCredentialParameter):
            return BitwardenLoginCredentialParameterModel(
                bitwarden_login_credential_parameter_id=parameter.bitwarden_login_credential_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                bitwarden_collection_id=parameter.bitwarden_collection_id,
                bitwarden_item_id=parameter.bitwarden_item_id,
                url_parameter_key=parameter.url_parameter_key,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, BitwardenSensitiveInformationParameter):
            return BitwardenSensitiveInformationParameterModel(
                bitwarden_sensitive_information_parameter_id=parameter.bitwarden_sensitive_information_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                bitwarden_collection_id=parameter.bitwarden_collection_id,
                bitwarden_identity_key=parameter.bitwarden_identity_key,
                bitwarden_identity_fields=parameter.bitwarden_identity_fields,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, BitwardenCreditCardDataParameter):
            return BitwardenCreditCardDataParameterModel(
                bitwarden_credit_card_data_parameter_id=parameter.bitwarden_credit_card_data_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                bitwarden_collection_id=parameter.bitwarden_collection_id,
                bitwarden_item_id=parameter.bitwarden_item_id,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, CredentialParameter):
            return CredentialParameterModel(
                credential_parameter_id=parameter.credential_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                credential_id=parameter.credential_id,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, OnePasswordCredentialParameter):
            return OnePasswordCredentialParameterModel(
                onepassword_credential_parameter_id=parameter.onepassword_credential_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                vault_id=parameter.vault_id,
                item_id=parameter.item_id,
                deleted_at=parameter.deleted_at,
            )
        elif isinstance(parameter, AzureVaultCredentialParameter):
            return AzureVaultCredentialParameterModel(
                azure_vault_credential_parameter_id=parameter.azure_vault_credential_parameter_id,
                workflow_id=parameter.workflow_id,
                key=parameter.key,
                description=parameter.description,
                vault_name=parameter.vault_name,
                username_key=parameter.username_key,
                password_key=parameter.password_key,
                totp_secret_key=parameter.totp_secret_key,
                deleted_at=parameter.deleted_at,
            )
        else:
            raise ValueError(f"Unsupported credential parameter type: {type(parameter).__name__}")
