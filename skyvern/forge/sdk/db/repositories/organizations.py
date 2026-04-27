from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, overload

from sqlalchemy import select, update

from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db.base_alchemy_db import read_retry
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import (
    OrganizationAuthTokenModel,
    OrganizationModel,
    TaskModel,
    WorkflowRunModel,
)
from skyvern.forge.sdk.db.utils import (
    convert_to_organization,
    convert_to_organization_auth_token,
)
from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.schemas.organizations import (
    AzureClientSecretCredential,
    AzureOrganizationAuthToken,
    BitwardenCredential,
    BitwardenOrganizationAuthToken,
    Organization,
    OrganizationAuthToken,
)
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus


class OrganizationsRepository(BaseRepository):
    """Database operations for organization and auth-token management."""

    @read_retry()
    @db_operation("get_active_verification_requests", log_errors=False)
    async def get_active_verification_requests(self, organization_id: str) -> list[dict]:
        """Return active 2FA verification requests for an organization.

        Queries both tasks and workflow runs where waiting_for_verification_code=True.
        Used to provide initial state when a WebSocket notification client connects.
        """
        results: list[dict] = []
        async with self.Session() as session:
            # Tasks waiting for verification (exclude finalized tasks)
            finalized_task_statuses = [s.value for s in TaskStatus if s.is_final()]
            task_rows = (
                await session.scalars(
                    select(TaskModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(waiting_for_verification_code=True)
                    .filter_by(workflow_run_id=None)
                    .filter(TaskModel.status.not_in(finalized_task_statuses))
                    .filter(TaskModel.created_at > datetime.now(timezone.utc) - timedelta(hours=1))
                )
            ).all()
            for t in task_rows:
                results.append(
                    {
                        "task_id": t.task_id,
                        "workflow_run_id": None,
                        "verification_code_identifier": t.verification_code_identifier,
                        "verification_code_polling_started_at": (
                            t.verification_code_polling_started_at.isoformat()
                            if t.verification_code_polling_started_at
                            else None
                        ),
                    }
                )
            # Workflow runs waiting for verification (exclude finalized runs)
            finalized_wr_statuses = [s.value for s in WorkflowRunStatus if s.is_final()]
            wr_rows = (
                await session.scalars(
                    select(WorkflowRunModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(waiting_for_verification_code=True)
                    .filter(WorkflowRunModel.status.not_in(finalized_wr_statuses))
                    .filter(WorkflowRunModel.created_at > datetime.now(timezone.utc) - timedelta(hours=1))
                )
            ).all()
            for wr in wr_rows:
                results.append(
                    {
                        "task_id": None,
                        "workflow_run_id": wr.workflow_run_id,
                        "verification_code_identifier": wr.verification_code_identifier,
                        "verification_code_polling_started_at": (
                            wr.verification_code_polling_started_at.isoformat()
                            if wr.verification_code_polling_started_at
                            else None
                        ),
                    }
                )
        return results

    @db_operation("get_all_organizations")
    async def get_all_organizations(self) -> list[Organization]:
        async with self.Session() as session:
            organizations = (await session.scalars(select(OrganizationModel))).all()
            return [convert_to_organization(organization) for organization in organizations]

    @db_operation("get_organization")
    async def get_organization(self, organization_id: str) -> Organization | None:
        async with self.Session() as session:
            if organization := (
                await session.scalars(select(OrganizationModel).filter_by(organization_id=organization_id))
            ).first():
                return convert_to_organization(organization)
            else:
                return None

    @db_operation("get_organization_by_domain")
    async def get_organization_by_domain(self, domain: str) -> Organization | None:
        async with self.Session() as session:
            if organization := (await session.scalars(select(OrganizationModel).filter_by(domain=domain))).first():
                return convert_to_organization(organization)
            return None

    @db_operation("create_organization")
    async def create_organization(
        self,
        organization_name: str,
        webhook_callback_url: str | None = None,
        max_steps_per_run: int | None = None,
        max_retries_per_step: int | None = None,
        domain: str | None = None,
        organization_id: str | None = None,
    ) -> Organization:
        async with self.Session() as session:
            org = OrganizationModel(
                organization_id=organization_id,
                organization_name=organization_name,
                webhook_callback_url=webhook_callback_url,
                max_steps_per_run=max_steps_per_run,
                max_retries_per_step=max_retries_per_step,
                domain=domain,
            )
            session.add(org)
            await session.commit()
            await session.refresh(org)

        return convert_to_organization(org)

    @db_operation("update_organization")
    async def update_organization(
        self,
        organization_id: str,
        organization_name: str | None = None,
        webhook_callback_url: str | None = None,
        max_steps_per_run: int | None = None,
        max_retries_per_step: int | None = None,
        artifact_url_expiry_seconds: int | None = None,
        clear_artifact_url_expiry_seconds: bool = False,
    ) -> Organization:
        async with self.Session() as session:
            organization = (
                await session.scalars(select(OrganizationModel).filter_by(organization_id=organization_id))
            ).first()
            if not organization:
                raise NotFoundError
            if organization_name:
                organization.organization_name = organization_name
            if webhook_callback_url:
                organization.webhook_callback_url = webhook_callback_url
            if max_steps_per_run:
                organization.max_steps_per_run = max_steps_per_run
            if max_retries_per_step:
                organization.max_retries_per_step = max_retries_per_step
            # ``clear_*`` decouples "don't update" (None) from "explicitly clear":
            # callers pass ``clear_artifact_url_expiry_seconds=True`` to reset
            # the value to NULL, falling back to the global default.
            if clear_artifact_url_expiry_seconds:
                organization.artifact_url_expiry_seconds = None
            elif artifact_url_expiry_seconds is not None:
                organization.artifact_url_expiry_seconds = artifact_url_expiry_seconds
            await session.commit()
            await session.refresh(organization)
            return Organization.model_validate(organization)

    @overload
    async def get_valid_org_auth_token(
        self,
        organization_id: str,
        token_type: Literal["api", "onepassword_service_account", "custom_credential_service"],
    ) -> OrganizationAuthToken | None: ...

    @overload
    async def get_valid_org_auth_token(  # type: ignore
        self,
        organization_id: str,
        token_type: Literal["azure_client_secret_credential"],
    ) -> AzureOrganizationAuthToken | None: ...

    @overload
    async def get_valid_org_auth_token(  # type: ignore
        self,
        organization_id: str,
        token_type: Literal["bitwarden_credential"],
    ) -> BitwardenOrganizationAuthToken | None: ...

    @db_operation("get_valid_org_auth_token")
    async def get_valid_org_auth_token(
        self,
        organization_id: str,
        token_type: Literal[
            "api",
            "onepassword_service_account",
            "azure_client_secret_credential",
            "bitwarden_credential",
            "custom_credential_service",
        ],
    ) -> OrganizationAuthToken | AzureOrganizationAuthToken | BitwardenOrganizationAuthToken | None:
        async with self.Session() as session:
            if token := (
                await session.scalars(
                    select(OrganizationAuthTokenModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(token_type=token_type)
                    .filter_by(valid=True)
                    .order_by(OrganizationAuthTokenModel.created_at.desc())
                )
            ).first():
                return await convert_to_organization_auth_token(token, token_type)
            else:
                return None

    @db_operation("replace_org_auth_token")
    async def replace_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token: str | AzureClientSecretCredential | BitwardenCredential,
        encrypted_method: EncryptMethod | None = None,
    ) -> OrganizationAuthToken | AzureOrganizationAuthToken | BitwardenOrganizationAuthToken:
        """Atomically invalidate existing tokens and create a new one in a single transaction."""
        if token_type is OrganizationAuthTokenType.azure_client_secret_credential:
            if not isinstance(token, AzureClientSecretCredential):
                raise TypeError("Expected AzureClientSecretCredential for this token_type")
            plaintext_token = token.model_dump_json()
        elif token_type is OrganizationAuthTokenType.bitwarden_credential:
            if not isinstance(token, BitwardenCredential):
                raise TypeError("Expected BitwardenCredential for this token_type")
            plaintext_token = token.model_dump_json()
        else:
            if not isinstance(token, str):
                raise TypeError("Expected str token for this token_type")
            plaintext_token = token

        encrypted_token = ""
        if encrypted_method is not None:
            encrypted_token = await encryptor.encrypt(plaintext_token, encrypted_method)
            plaintext_token = ""

        async with self.Session() as session:
            # Invalidate existing tokens
            await session.execute(
                update(OrganizationAuthTokenModel)
                .filter_by(organization_id=organization_id)
                .filter_by(token_type=token_type)
                .filter_by(valid=True)
                .values(valid=False)
            )
            # Create new token
            auth_token = OrganizationAuthTokenModel(
                organization_id=organization_id,
                token_type=token_type,
                token=plaintext_token,
                encrypted_token=encrypted_token,
                encrypted_method=encrypted_method.value if encrypted_method is not None else "",
            )
            session.add(auth_token)
            await session.commit()
            await session.refresh(auth_token)

        return await convert_to_organization_auth_token(auth_token, token_type)

    @db_operation("get_valid_org_auth_tokens")
    async def get_valid_org_auth_tokens(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
    ) -> list[OrganizationAuthToken]:
        async with self.Session() as session:
            tokens = (
                await session.scalars(
                    select(OrganizationAuthTokenModel)
                    .filter_by(organization_id=organization_id)
                    .filter_by(token_type=token_type)
                    .filter_by(valid=True)
                    .order_by(OrganizationAuthTokenModel.created_at.desc())
                )
            ).all()
            return [await convert_to_organization_auth_token(token, token_type) for token in tokens]

    @db_operation("validate_org_auth_token")
    async def validate_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token: str,
        valid: bool | None = True,
        encrypted_method: EncryptMethod | None = None,
    ) -> OrganizationAuthToken | None:
        encrypted_token = ""
        if encrypted_method is not None:
            encrypted_token = await encryptor.encrypt(token, encrypted_method)

        async with self.Session() as session:
            query = (
                select(OrganizationAuthTokenModel)
                .filter_by(organization_id=organization_id)
                .filter_by(token_type=token_type)
            )
            if encrypted_token:
                query = query.filter_by(encrypted_token=encrypted_token)
            else:
                query = query.filter_by(token=token)
            if valid is not None:
                query = query.filter_by(valid=valid)
            if token_obj := (await session.scalars(query)).first():
                return await convert_to_organization_auth_token(token_obj, token_type)
            else:
                return None

    @db_operation("create_org_auth_token")
    async def create_org_auth_token(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
        token: str | AzureClientSecretCredential | BitwardenCredential,
        encrypted_method: EncryptMethod | None = None,
    ) -> OrganizationAuthToken | AzureOrganizationAuthToken | BitwardenOrganizationAuthToken:
        if token_type is OrganizationAuthTokenType.azure_client_secret_credential:
            if not isinstance(token, AzureClientSecretCredential):
                raise TypeError("Expected AzureClientSecretCredential for this token_type")
            plaintext_token = token.model_dump_json()
        elif token_type is OrganizationAuthTokenType.bitwarden_credential:
            if not isinstance(token, BitwardenCredential):
                raise TypeError("Expected BitwardenCredential for this token_type")
            plaintext_token = token.model_dump_json()
        else:
            if not isinstance(token, str):
                raise TypeError("Expected str token for this token_type")
            plaintext_token = token

        encrypted_token = ""

        if encrypted_method is not None:
            encrypted_token = await encryptor.encrypt(plaintext_token, encrypted_method)
            plaintext_token = ""

        async with self.Session() as session:
            auth_token = OrganizationAuthTokenModel(
                organization_id=organization_id,
                token_type=token_type,
                token=plaintext_token,
                encrypted_token=encrypted_token,
                encrypted_method=encrypted_method.value if encrypted_method is not None else "",
            )
            session.add(auth_token)
            await session.commit()
            await session.refresh(auth_token)

        return await convert_to_organization_auth_token(auth_token, token_type)

    @db_operation("invalidate_org_auth_tokens")
    async def invalidate_org_auth_tokens(
        self,
        organization_id: str,
        token_type: OrganizationAuthTokenType,
    ) -> None:
        """Invalidate all existing tokens of a specific type for an organization."""
        async with self.Session() as session:
            await session.execute(
                update(OrganizationAuthTokenModel)
                .filter_by(organization_id=organization_id)
                .filter_by(token_type=token_type)
                .filter_by(valid=True)
                .values(valid=False)
            )
            await session.commit()
