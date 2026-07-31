from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

from sqlalchemy import or_, select, update

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import CredentialModel, OrganizationBitwardenCollectionModel
from skyvern.forge.sdk.db.repositories.proxy_pin_update import apply_proxy_pin_to_model, normalize_proxy_pin_for_create
from skyvern.forge.sdk.db.utils import serialize_proxy_location
from skyvern.forge.sdk.schemas.credentials import (
    Credential,
    CredentialType,
    CredentialVaultType,
)
from skyvern.forge.sdk.schemas.organization_bitwarden_collections import OrganizationBitwardenCollection
from skyvern.schemas.proxy_pinning import generate_proxy_session_id, should_generate_proxy_session_id
from skyvern.schemas.runs import ProxyLocation, ProxyLocationInput

_UNSET = object()


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
        tested_url: str | None = None,
        proxy_location: ProxyLocationInput = None,
        proxy_session_id: str | None = None,
    ) -> Credential:
        proxy_location, proxy_session_id = normalize_proxy_pin_for_create(
            proxy_location=proxy_location,
            proxy_session_id=proxy_session_id,
        )
        serialized_proxy_location = serialize_proxy_location(proxy_location)
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
                tested_url=tested_url,
                proxy_location=serialized_proxy_location,
                proxy_session_id=proxy_session_id,
            )
            session.add(credential)
            await session.flush()
            if should_generate_proxy_session_id(proxy_location) and credential.proxy_session_id is None:
                credential.proxy_session_id = generate_proxy_session_id(credential.credential_id)
            await session.commit()
            await session.refresh(credential)
            return Credential.model_validate(credential)

    @db_operation("get_credentials_by_browser_profile_id")
    async def get_credentials_by_browser_profile_id(
        self, browser_profile_id: str, organization_id: str
    ) -> list[Credential]:
        async with self.Session() as session:
            credentials = (
                await session.scalars(
                    select(CredentialModel)
                    .filter_by(browser_profile_id=browser_profile_id)
                    .filter_by(organization_id=organization_id)
                    .filter(CredentialModel.deleted_at.is_(None))
                )
            ).all()
            return [Credential.model_validate(c) for c in credentials]

    @db_operation("link_browser_profile_if_unset")
    async def link_browser_profile_if_unset(
        self, credential_id: str, organization_id: str, browser_profile_id: str
    ) -> str | None:
        """Atomically link a browser profile only if the credential has none yet. Returns the WINNING
        profile id — the one just set if we won the race, or the existing one if a concurrent first
        login already linked a different profile. None means the credential is gone. Lets the auto-create
        path drop its orphan and adopt the winner instead of clobbering it (last-writer-wins)."""
        async with self.Session() as session:
            result = await session.execute(
                update(CredentialModel)
                .where(CredentialModel.credential_id == credential_id)
                .where(CredentialModel.organization_id == organization_id)
                .where(CredentialModel.deleted_at.is_(None))
                .where(CredentialModel.browser_profile_id.is_(None))
                .values(browser_profile_id=browser_profile_id)
            )
            await session.commit()
            if result.rowcount == 1:
                return browser_profile_id
            credential = (
                await session.scalars(
                    select(CredentialModel)
                    .filter_by(credential_id=credential_id)
                    .filter_by(organization_id=organization_id)
                    .filter(CredentialModel.deleted_at.is_(None))
                )
            ).first()
            return credential.browser_profile_id if credential else None

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

    @db_operation("get_credentials_by_ids")
    async def get_credentials_by_ids(self, credential_ids: list[str], organization_id: str) -> list[Credential]:
        if not credential_ids:
            return []
        async with self.Session() as session:
            credentials = (
                await session.scalars(
                    select(CredentialModel)
                    .filter(CredentialModel.credential_id.in_(credential_ids))
                    .filter_by(organization_id=organization_id)
                    .filter(CredentialModel.deleted_at.is_(None))
                )
            ).all()
            return [Credential.model_validate(credential) for credential in credentials]

    @db_operation("get_credentials")
    async def get_credentials(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        vault_type: str | None = None,
        credential_type: str | None = None,
        search: str | None = None,
        folder_id: str | None = None,
    ) -> list[Credential]:
        async with self.Session() as session:
            query = (
                select(CredentialModel)
                .filter_by(organization_id=organization_id)
                .filter(CredentialModel.deleted_at.is_(None))
            )
            if vault_type is not None:
                query = query.filter(CredentialModel.vault_type == vault_type)
            if credential_type is not None:
                query = query.filter(CredentialModel.credential_type == credential_type)
            if folder_id is not None:
                query = query.filter(CredentialModel.folder_id == folder_id)
            if search:
                # Escape LIKE wildcards so a literal % or _ in the query narrows rather than broadens.
                escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                search_pattern = f"%{escaped}%"
                query = query.filter(
                    or_(
                        CredentialModel.name.ilike(search_pattern, escape="\\"),
                        CredentialModel.username.ilike(search_pattern, escape="\\"),
                        CredentialModel.secret_label.ilike(search_pattern, escape="\\"),
                        CredentialModel.card_brand.ilike(search_pattern, escape="\\"),
                        CredentialModel.card_last4.ilike(search_pattern, escape="\\"),
                    )
                )
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
        pin_saved_session_ip: bool | None = None,
        tested_url: str | None = None,
        user_context: str | None = None,
        save_browser_session_intent: bool | None = None,
        run_sequentially: bool | None = None,
        proxy_location: ProxyLocationInput | object = _UNSET,
        proxy_session_id: str | None | object = _UNSET,
        rotate_proxy_session_id: bool = False,
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
            # Sentinel-gated so an explicit None unlinks (user picks Auto after attaching a profile),
            # while an omitted value (_UNSET) leaves the link untouched.
            if browser_profile_id is not _UNSET:
                credential.browser_profile_id = cast("str | None", browser_profile_id)
            if pin_saved_session_ip is not None:
                credential.pin_saved_session_ip = pin_saved_session_ip
            if tested_url is not None:
                credential.tested_url = tested_url
            if user_context is not None:
                credential.user_context = user_context
            if save_browser_session_intent is not None:
                credential.save_browser_session_intent = save_browser_session_intent
            if run_sequentially is not None:
                credential.run_sequentially = run_sequentially
            apply_proxy_pin_to_model(
                credential,
                entity_id=credential_id,
                proxy_location=proxy_location,
                proxy_session_id=proxy_session_id,
                unset=_UNSET,
                rotate_proxy_session_id=rotate_proxy_session_id,
            )
            # The IP pin holds a stable IP via a sticky proxy session. When the pin is turned on but no
            # proxy session is provisioned (pin set without a residential-ISP proxy), mint one now so
            # pin=true is not a silent no-op when a run is later seeded from this credential's profile.
            if pin_saved_session_ip and not credential.proxy_session_id:
                credential.proxy_location = serialize_proxy_location(ProxyLocation.RESIDENTIAL_ISP)
                credential.proxy_session_id = generate_proxy_session_id(credential_id)
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
        tested_url: str | None = None,
        proxy_location: ProxyLocationInput | object = _UNSET,
        proxy_session_id: str | None | object = _UNSET,
        rotate_proxy_session_id: bool = False,
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
            if tested_url is not None:
                credential.tested_url = tested_url
            apply_proxy_pin_to_model(
                credential,
                entity_id=credential_id,
                proxy_location=proxy_location,
                proxy_session_id=proxy_session_id,
                unset=_UNSET,
                rotate_proxy_session_id=rotate_proxy_session_id,
            )
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
