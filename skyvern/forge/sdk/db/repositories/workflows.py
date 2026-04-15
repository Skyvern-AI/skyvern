from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import structlog
from sqlalchemy import exists, func, or_, select, update

from skyvern.constants import DEFAULT_SCRIPT_RUN_ID
from skyvern.forge.sdk.db._error_handling import db_operation
from skyvern.forge.sdk.db._sentinels import _UNSET
from skyvern.forge.sdk.db._soft_delete import exclude_deleted
from skyvern.forge.sdk.db.base_repository import BaseRepository
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.models import (
    AWSSecretParameterModel,
    AzureVaultCredentialParameterModel,
    BitwardenCreditCardDataParameterModel,
    BitwardenLoginCredentialParameterModel,
    BitwardenSensitiveInformationParameterModel,
    CredentialParameterModel,
    FolderModel,
    OnePasswordCredentialParameterModel,
    OutputParameterModel,
    WorkflowModel,
    WorkflowParameterModel,
    WorkflowRunModel,
    WorkflowScheduleModel,
    WorkflowTemplateModel,
)
from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.db.utils import convert_to_workflow, serialize_proxy_location
from skyvern.forge.sdk.workflow.models.block import Block, ForLoopBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.schemas.runs import ProxyLocationInput
from skyvern.schemas.workflows import WorkflowStatus

LOG = structlog.get_logger()


def _align_block_output_parameters(workflow_definition: WorkflowDefinition) -> None:
    """Rebind each block's ``output_parameter`` to the reconciled instance
    from ``workflow_definition.parameters`` by key, recursing into
    ``ForLoopBlock.loop_blocks``.

    The reconcile helper mutates IDs on the top-level parameters list only.
    When a caller round-trips the definition through
    ``model_validate(model_dump(...))`` or constructs blocks with fresh
    ``OutputParameter`` instances, the block-level field is a distinct
    object from the top-level entry and won't pick up the reconciled ID
    unless we rebind it here.
    """
    key_to_output_parameter: dict[str, OutputParameter] = {
        p.key: p for p in workflow_definition.parameters if isinstance(p, OutputParameter)
    }
    if not key_to_output_parameter:
        return

    def _visit(blocks: list[Block]) -> None:
        for block in blocks:
            canonical = key_to_output_parameter.get(block.output_parameter.key)
            if canonical is not None and canonical is not block.output_parameter:
                block.output_parameter = canonical
            if isinstance(block, ForLoopBlock):
                _visit(block.loop_blocks)

    _visit(workflow_definition.blocks)


class WorkflowsRepository(BaseRepository):
    """Database operations for workflow management."""

    @db_operation("create_workflow")
    async def create_workflow(
        self,
        title: str,
        workflow_definition: dict[str, Any],
        organization_id: str | None = None,
        description: str | None = None,
        proxy_location: ProxyLocationInput = None,
        webhook_callback_url: str | None = None,
        max_screenshot_scrolling_times: int | None = None,
        extra_http_headers: dict[str, str] | None = None,
        totp_verification_url: str | None = None,
        totp_identifier: str | None = None,
        persist_browser_session: bool = False,
        model: dict[str, Any] | None = None,
        workflow_permanent_id: str | None = None,
        version: int | None = None,
        is_saved_task: bool = False,
        status: WorkflowStatus = WorkflowStatus.published,
        run_with: str | None = None,
        ai_fallback: bool = True,
        cache_key: str | None = None,
        adaptive_caching: bool = False,
        code_version: int | None = None,
        generate_script_on_terminal: bool = False,
        run_sequentially: bool = False,
        sequential_key: str | None = None,
        folder_id: str | None = None,
    ) -> Workflow:
        async with self.Session() as session:
            workflow = WorkflowModel(
                organization_id=organization_id,
                title=title,
                description=description,
                workflow_definition=workflow_definition,
                proxy_location=serialize_proxy_location(proxy_location),
                webhook_callback_url=webhook_callback_url,
                totp_verification_url=totp_verification_url,
                totp_identifier=totp_identifier,
                max_screenshot_scrolling_times=max_screenshot_scrolling_times,
                extra_http_headers=extra_http_headers,
                persist_browser_session=persist_browser_session,
                model=model,
                is_saved_task=is_saved_task,
                status=status,
                run_with=run_with,
                ai_fallback=ai_fallback,
                cache_key=cache_key or DEFAULT_SCRIPT_RUN_ID,
                adaptive_caching=adaptive_caching,
                code_version=code_version,
                generate_script_on_terminal=generate_script_on_terminal,
                run_sequentially=run_sequentially,
                sequential_key=sequential_key,
                folder_id=folder_id,
            )
            if workflow_permanent_id:
                workflow.workflow_permanent_id = workflow_permanent_id
            if version:
                workflow.version = version
            session.add(workflow)

            # Update folder's modified_at if folder_id is provided
            if folder_id:
                # Validate folder exists and belongs to the same organization
                folder_stmt = (
                    select(FolderModel)
                    .where(FolderModel.folder_id == folder_id)
                    .where(FolderModel.organization_id == organization_id)
                    .where(FolderModel.deleted_at.is_(None))
                )
                folder_model = await session.scalar(folder_stmt)
                if not folder_model:
                    raise ValueError(
                        f"Folder {folder_id} not found or does not belong to organization {organization_id}"
                    )
                folder_model.modified_at = datetime.now(timezone.utc)

            await session.commit()
            await session.refresh(workflow)
            return convert_to_workflow(workflow, self.debug_enabled)

    @db_operation("soft_delete_workflow_by_id")
    async def soft_delete_workflow_by_id(self, workflow_id: str, organization_id: str) -> None:
        async with self.Session() as session:
            # soft delete the workflow by setting the deleted_at field to the current time
            update_deleted_at_query = (
                update(WorkflowModel)
                .where(WorkflowModel.workflow_id == workflow_id)
                .where(WorkflowModel.organization_id == organization_id)
                .where(WorkflowModel.deleted_at.is_(None))
                .values(deleted_at=datetime.now(timezone.utc))
            )
            await session.execute(update_deleted_at_query)
            await session.commit()

    @db_operation("get_workflow")
    async def get_workflow(self, workflow_id: str, organization_id: str | None = None) -> Workflow | None:
        async with self.Session() as session:
            get_workflow_query = exclude_deleted(
                select(WorkflowModel).filter_by(workflow_id=workflow_id), WorkflowModel
            )
            if organization_id:
                get_workflow_query = get_workflow_query.filter_by(organization_id=organization_id)
            if workflow := (await session.scalars(get_workflow_query)).first():
                is_template = (
                    await self.is_workflow_template(
                        workflow_permanent_id=workflow.workflow_permanent_id,
                        organization_id=workflow.organization_id,
                    )
                    if organization_id
                    else False
                )
                return convert_to_workflow(
                    workflow,
                    self.debug_enabled,
                    is_template=is_template,
                )
            return None

    @db_operation("get_workflow_by_permanent_id")
    async def get_workflow_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        version: int | None = None,
        ignore_version: int | None = None,
        filter_deleted: bool = True,
    ) -> Workflow | None:
        get_workflow_query = select(WorkflowModel).filter_by(workflow_permanent_id=workflow_permanent_id)
        if filter_deleted:
            get_workflow_query = get_workflow_query.filter(WorkflowModel.deleted_at.is_(None))
        if organization_id:
            get_workflow_query = get_workflow_query.filter_by(organization_id=organization_id)
        if version:
            get_workflow_query = get_workflow_query.filter_by(version=version)
        if ignore_version:
            get_workflow_query = get_workflow_query.filter(WorkflowModel.version != ignore_version)
        get_workflow_query = get_workflow_query.order_by(WorkflowModel.version.desc())
        async with self.Session() as session:
            if workflow := (await session.scalars(get_workflow_query)).first():
                is_template = (
                    await self.is_workflow_template(
                        workflow_permanent_id=workflow.workflow_permanent_id,
                        organization_id=workflow.organization_id,
                    )
                    if organization_id
                    else False
                )
                return convert_to_workflow(
                    workflow,
                    self.debug_enabled,
                    is_template=is_template,
                )
            return None

    @db_operation("get_workflow_for_workflow_run")
    async def get_workflow_for_workflow_run(
        self,
        workflow_run_id: str,
        organization_id: str | None = None,
        filter_deleted: bool = True,
    ) -> Workflow | None:
        get_workflow_query = select(WorkflowModel)

        if filter_deleted:
            get_workflow_query = get_workflow_query.filter(WorkflowModel.deleted_at.is_(None))

        get_workflow_query = get_workflow_query.join(
            WorkflowRunModel,
            WorkflowRunModel.workflow_id == WorkflowModel.workflow_id,
        )

        if organization_id:
            get_workflow_query = get_workflow_query.filter(WorkflowRunModel.organization_id == organization_id)

        get_workflow_query = get_workflow_query.filter(WorkflowRunModel.workflow_run_id == workflow_run_id)
        async with self.Session() as session:
            if workflow := (await session.scalars(get_workflow_query)).first():
                is_template = (
                    await self.is_workflow_template(
                        workflow_permanent_id=workflow.workflow_permanent_id,
                        organization_id=workflow.organization_id,
                    )
                    if organization_id
                    else False
                )
                return convert_to_workflow(
                    workflow,
                    self.debug_enabled,
                    is_template=is_template,
                )
            return None

    @db_operation("get_workflow_versions_by_permanent_id")
    async def get_workflow_versions_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
        filter_deleted: bool = True,
    ) -> list[Workflow]:
        """
        Get all versions of a workflow by its permanent ID, ordered by version descending (newest first).
        """
        get_workflows_query = select(WorkflowModel).filter_by(workflow_permanent_id=workflow_permanent_id)
        if filter_deleted:
            get_workflows_query = get_workflows_query.filter(WorkflowModel.deleted_at.is_(None))
        if organization_id:
            get_workflows_query = get_workflows_query.filter_by(organization_id=organization_id)
        get_workflows_query = get_workflows_query.order_by(WorkflowModel.version.desc())

        async with self.Session() as session:
            workflows = (await session.scalars(get_workflows_query)).all()
            template_permanent_ids: set[str] = set()
            if workflows and organization_id:
                template_permanent_ids = await self.get_org_template_permanent_ids(organization_id)

            return [
                convert_to_workflow(
                    workflow,
                    self.debug_enabled,
                    is_template=workflow.workflow_permanent_id in template_permanent_ids,
                )
                for workflow in workflows
            ]

    @db_operation("get_workflows_by_permanent_ids")
    async def get_workflows_by_permanent_ids(
        self,
        workflow_permanent_ids: list[str],
        organization_id: str | None = None,
        page: int = 1,
        page_size: int = 10,
        title: str = "",
        statuses: list[WorkflowStatus] | None = None,
    ) -> list[Workflow]:
        """
        Get all workflows with the latest version for the organization.
        """
        if page < 1:
            raise ValueError(f"Page must be greater than 0, got {page}")
        db_page = page - 1
        async with self.Session() as session:
            subquery = (
                select(
                    WorkflowModel.workflow_permanent_id,
                    func.max(WorkflowModel.version).label("max_version"),
                )
                .where(WorkflowModel.workflow_permanent_id.in_(workflow_permanent_ids))
                .where(WorkflowModel.deleted_at.is_(None))
                .group_by(
                    WorkflowModel.workflow_permanent_id,
                )
                .subquery()
            )
            main_query = select(WorkflowModel).join(
                subquery,
                (WorkflowModel.workflow_permanent_id == subquery.c.workflow_permanent_id)
                & (WorkflowModel.version == subquery.c.max_version),
            )
            if organization_id:
                main_query = main_query.where(WorkflowModel.organization_id == organization_id)
            if title:
                main_query = main_query.where(WorkflowModel.title.ilike(f"%{title}%"))
            if statuses:
                main_query = main_query.where(WorkflowModel.status.in_(statuses))
            main_query = (
                main_query.order_by(WorkflowModel.created_at.desc()).limit(page_size).offset(db_page * page_size)
            )
            workflows = (await session.scalars(main_query)).all()

            # Map template status by permanent_id so API responses surface is_template
            template_permanent_ids: set[str] = set()
            if workflows and organization_id:
                template_permanent_ids = await self.get_org_template_permanent_ids(organization_id)

            return [
                convert_to_workflow(
                    workflow,
                    self.debug_enabled,
                    is_template=workflow.workflow_permanent_id in template_permanent_ids,
                )
                for workflow in workflows
            ]

    @db_operation("get_workflows_by_organization_id")
    async def get_workflows_by_organization_id(
        self,
        organization_id: str,
        page: int = 1,
        page_size: int = 10,
        only_saved_tasks: bool = False,
        only_workflows: bool = False,
        only_templates: bool = False,
        search_key: str | None = None,
        folder_id: str | None = None,
        statuses: list[WorkflowStatus] | None = None,
    ) -> list[Workflow]:
        """
        Get all workflows with the latest version for the organization.

        Search semantics:
        - If `search_key` is provided, its value is used as a unified search term for
          `workflows.title`, `workflows.workflow_permanent_id`, `folders.title`, and
          workflow parameter metadata (key, description, and default_value).
        - If `search_key` is not provided, no search filtering is applied.
        - Parameter metadata search excludes soft-deleted parameter rows across parameter tables.
        """
        if page < 1:
            raise ValueError(f"Page must be greater than 0, got {page}")
        db_page = page - 1
        async with self.Session() as session:
            subquery = (
                select(
                    WorkflowModel.organization_id,
                    WorkflowModel.workflow_permanent_id,
                    func.max(WorkflowModel.version).label("max_version"),
                )
                .where(WorkflowModel.organization_id == organization_id)
                .where(WorkflowModel.deleted_at.is_(None))
                .group_by(
                    WorkflowModel.organization_id,
                    WorkflowModel.workflow_permanent_id,
                )
                .subquery()
            )
            main_query = (
                select(WorkflowModel)
                .join(
                    subquery,
                    (WorkflowModel.organization_id == subquery.c.organization_id)
                    & (WorkflowModel.workflow_permanent_id == subquery.c.workflow_permanent_id)
                    & (WorkflowModel.version == subquery.c.max_version),
                )
                .outerjoin(
                    FolderModel,
                    (WorkflowModel.folder_id == FolderModel.folder_id)
                    & (FolderModel.organization_id == WorkflowModel.organization_id),
                )
            )
            if only_saved_tasks:
                main_query = main_query.where(WorkflowModel.is_saved_task.is_(True))
            elif only_workflows:
                main_query = main_query.where(WorkflowModel.is_saved_task.is_(False))
            if only_templates:
                # Filter by workflow_templates table (templates at permanent_id level)
                template_subquery = select(WorkflowTemplateModel.workflow_permanent_id).where(
                    WorkflowTemplateModel.organization_id == organization_id,
                    WorkflowTemplateModel.deleted_at.is_(None),
                )
                main_query = main_query.where(WorkflowModel.workflow_permanent_id.in_(template_subquery))
            if statuses:
                main_query = main_query.where(WorkflowModel.status.in_(statuses))
            if folder_id:
                main_query = main_query.where(WorkflowModel.folder_id == folder_id)
            if search_key:
                search_like = f"%{search_key}%"
                title_like = WorkflowModel.title.ilike(search_like)
                folder_title_like = FolderModel.title.ilike(search_like)
                workflow_permanent_id_like = WorkflowModel.workflow_permanent_id.icontains(search_key, autoescape=True)

                parameter_filters = [
                    # WorkflowParameterModel
                    exists(
                        select(1)
                        .select_from(WorkflowParameterModel)
                        .where(WorkflowParameterModel.workflow_id == WorkflowModel.workflow_id)
                        .where(WorkflowParameterModel.deleted_at.is_(None))
                        .where(
                            or_(
                                WorkflowParameterModel.key.ilike(search_like),
                                WorkflowParameterModel.description.ilike(search_like),
                                WorkflowParameterModel.default_value.ilike(search_like),
                            )
                        )
                    ),
                    # OutputParameterModel
                    exists(
                        select(1)
                        .select_from(OutputParameterModel)
                        .where(OutputParameterModel.workflow_id == WorkflowModel.workflow_id)
                        .where(OutputParameterModel.deleted_at.is_(None))
                        .where(
                            or_(
                                OutputParameterModel.key.ilike(search_like),
                                OutputParameterModel.description.ilike(search_like),
                            )
                        )
                    ),
                    # AWSSecretParameterModel
                    exists(
                        select(1)
                        .select_from(AWSSecretParameterModel)
                        .where(AWSSecretParameterModel.workflow_id == WorkflowModel.workflow_id)
                        .where(AWSSecretParameterModel.deleted_at.is_(None))
                        .where(
                            or_(
                                AWSSecretParameterModel.key.ilike(search_like),
                                AWSSecretParameterModel.description.ilike(search_like),
                            )
                        )
                    ),
                    # BitwardenLoginCredentialParameterModel
                    exists(
                        select(1)
                        .select_from(BitwardenLoginCredentialParameterModel)
                        .where(BitwardenLoginCredentialParameterModel.workflow_id == WorkflowModel.workflow_id)
                        .where(BitwardenLoginCredentialParameterModel.deleted_at.is_(None))
                        .where(
                            or_(
                                BitwardenLoginCredentialParameterModel.key.ilike(search_like),
                                BitwardenLoginCredentialParameterModel.description.ilike(search_like),
                            )
                        )
                    ),
                    # BitwardenSensitiveInformationParameterModel
                    exists(
                        select(1)
                        .select_from(BitwardenSensitiveInformationParameterModel)
                        .where(BitwardenSensitiveInformationParameterModel.workflow_id == WorkflowModel.workflow_id)
                        .where(BitwardenSensitiveInformationParameterModel.deleted_at.is_(None))
                        .where(
                            or_(
                                BitwardenSensitiveInformationParameterModel.key.ilike(search_like),
                                BitwardenSensitiveInformationParameterModel.description.ilike(search_like),
                            )
                        )
                    ),
                    # BitwardenCreditCardDataParameterModel
                    exists(
                        select(1)
                        .select_from(BitwardenCreditCardDataParameterModel)
                        .where(BitwardenCreditCardDataParameterModel.workflow_id == WorkflowModel.workflow_id)
                        .where(BitwardenCreditCardDataParameterModel.deleted_at.is_(None))
                        .where(
                            or_(
                                BitwardenCreditCardDataParameterModel.key.ilike(search_like),
                                BitwardenCreditCardDataParameterModel.description.ilike(search_like),
                            )
                        )
                    ),
                    # OnePasswordCredentialParameterModel
                    exists(
                        select(1)
                        .select_from(OnePasswordCredentialParameterModel)
                        .where(OnePasswordCredentialParameterModel.workflow_id == WorkflowModel.workflow_id)
                        .where(OnePasswordCredentialParameterModel.deleted_at.is_(None))
                        .where(
                            or_(
                                OnePasswordCredentialParameterModel.key.ilike(search_like),
                                OnePasswordCredentialParameterModel.description.ilike(search_like),
                            )
                        )
                    ),
                    # AzureVaultCredentialParameterModel
                    exists(
                        select(1)
                        .select_from(AzureVaultCredentialParameterModel)
                        .where(AzureVaultCredentialParameterModel.workflow_id == WorkflowModel.workflow_id)
                        .where(AzureVaultCredentialParameterModel.deleted_at.is_(None))
                        .where(
                            or_(
                                AzureVaultCredentialParameterModel.key.ilike(search_like),
                                AzureVaultCredentialParameterModel.description.ilike(search_like),
                            )
                        )
                    ),
                    # CredentialParameterModel
                    exists(
                        select(1)
                        .select_from(CredentialParameterModel)
                        .where(CredentialParameterModel.workflow_id == WorkflowModel.workflow_id)
                        .where(CredentialParameterModel.deleted_at.is_(None))
                        .where(
                            or_(
                                CredentialParameterModel.key.ilike(search_like),
                                CredentialParameterModel.description.ilike(search_like),
                            )
                        )
                    ),
                ]
                main_query = main_query.where(
                    or_(title_like, folder_title_like, workflow_permanent_id_like, or_(*parameter_filters))
                )
            main_query = (
                main_query.order_by(WorkflowModel.created_at.desc()).limit(page_size).offset(db_page * page_size)
            )
            workflows = (await session.scalars(main_query)).all()
            template_permanent_ids: set[str] = set()
            if workflows and organization_id:
                template_permanent_ids = await self.get_org_template_permanent_ids(organization_id)

            return [
                convert_to_workflow(
                    workflow,
                    self.debug_enabled,
                    is_template=workflow.workflow_permanent_id in template_permanent_ids,
                )
                for workflow in workflows
            ]

    @db_operation("update_workflow")
    async def update_workflow(
        self,
        workflow_id: str,
        organization_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        workflow_definition: dict[str, Any] | None = None,
        version: int | None = None,
        run_with: str | None = None,
        cache_key: str | None = None,
        status: str | None = None,
        import_error: str | None = None,
        proxy_location: ProxyLocationInput | object = _UNSET,
        webhook_callback_url: str | None | object = _UNSET,
        persist_browser_session: bool | None = None,
        model: dict[str, Any] | None | object = _UNSET,
        max_screenshot_scrolling_times: int | None | object = _UNSET,
        extra_http_headers: dict[str, str] | None | object = _UNSET,
        ai_fallback: bool | None = None,
        run_sequentially: bool | None = None,
        sequential_key: str | None | object = _UNSET,
    ) -> Workflow:
        async with self.Session() as session:
            get_workflow_query = exclude_deleted(
                select(WorkflowModel).filter_by(workflow_id=workflow_id), WorkflowModel
            )
            if organization_id:
                get_workflow_query = get_workflow_query.filter_by(organization_id=organization_id)
            if workflow := (await session.scalars(get_workflow_query)).first():
                if title is not None:
                    workflow.title = title
                if description is not None:
                    workflow.description = description
                if workflow_definition is not None:
                    workflow.workflow_definition = workflow_definition
                if version is not None:
                    workflow.version = version
                if run_with is not None:
                    workflow.run_with = run_with
                if cache_key is not None:
                    workflow.cache_key = cache_key
                if status is not None:
                    workflow.status = status
                if import_error is not None:
                    workflow.import_error = import_error
                if proxy_location is not _UNSET:
                    workflow.proxy_location = serialize_proxy_location(cast(ProxyLocationInput, proxy_location))
                if webhook_callback_url is not _UNSET:
                    workflow.webhook_callback_url = webhook_callback_url
                if persist_browser_session is not None:
                    workflow.persist_browser_session = persist_browser_session
                if model is not _UNSET:
                    workflow.model = model
                if max_screenshot_scrolling_times is not _UNSET:
                    workflow.max_screenshot_scrolling_times = max_screenshot_scrolling_times
                if extra_http_headers is not _UNSET:
                    workflow.extra_http_headers = extra_http_headers
                if ai_fallback is not None:
                    workflow.ai_fallback = ai_fallback
                if run_sequentially is not None:
                    workflow.run_sequentially = run_sequentially
                if sequential_key is not _UNSET:
                    workflow.sequential_key = sequential_key
                await session.commit()
                await session.refresh(workflow)
                is_template = (
                    await self.is_workflow_template(
                        workflow_permanent_id=workflow.workflow_permanent_id,
                        organization_id=workflow.organization_id,
                    )
                    if organization_id
                    else False
                )
                return convert_to_workflow(
                    workflow,
                    self.debug_enabled,
                    is_template=is_template,
                )
            else:
                raise NotFoundError("Workflow not found")

    @db_operation("update_workflow_and_reconcile_definition_params")
    async def update_workflow_and_reconcile_definition_params(
        self,
        workflow_id: str,
        organization_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        workflow_definition: WorkflowDefinition | None = None,
        version: int | None = None,
        run_with: str | None = None,
        cache_key: str | None = None,
        status: str | None = None,
        import_error: str | None = None,
        proxy_location: ProxyLocationInput | object = _UNSET,
        webhook_callback_url: str | None | object = _UNSET,
        persist_browser_session: bool | None = None,
        model: dict[str, Any] | None | object = _UNSET,
        max_screenshot_scrolling_times: int | None | object = _UNSET,
        extra_http_headers: dict[str, str] | None | object = _UNSET,
        ai_fallback: bool | None = None,
        run_sequentially: bool | None = None,
        sequential_key: str | None | object = _UNSET,
    ) -> Workflow:
        """One-session, one-commit update of the workflow row + definition-parameter rows.

        Reconciles ``WorkflowParameter`` and ``OutputParameter`` rows in the
        same session as the JSON dump so those two stay aligned.

        Credential-subclass parameters (``AWSSecretParameter``, ``Bitwarden*``,
        ``CredentialParameter``, ``OnePasswordCredentialParameter``,
        ``AzureVaultCredentialParameter``) and ``ContextParameter`` are
        intentionally out of scope: runtime resolves them off the JSON
        ``workflow_definition`` column (see
        ``WorkflowService._resolve_login_block_browser_profile_id`` which
        reads ``credential_id`` directly off the ``CredentialParameter``
        pydantic instance), not by joining the credential side tables.  The
        side tables remain populated by the YAML create path via
        ``save_workflow_definition_parameters`` and are used for workflow
        search/metadata; in-place edits via this method may leave the
        side-table metadata stale relative to the JSON until the next YAML
        round-trip.  That trade-off is deliberate — the alternative (sync
        every credential subclass's columns on every write) pulls a
        significant amount of orthogonal logic into this path.
        """
        async with self.Session() as session:
            get_workflow_query = exclude_deleted(
                select(WorkflowModel).filter_by(workflow_id=workflow_id), WorkflowModel
            )
            if organization_id:
                get_workflow_query = get_workflow_query.filter_by(organization_id=organization_id)
            workflow = (await session.scalars(get_workflow_query)).first()
            if not workflow:
                raise NotFoundError("Workflow not found")

            if title is not None:
                workflow.title = title
            if description is not None:
                workflow.description = description
            # Reconcile first: it mutates parameter IDs to match preserved DB
            # rows, so the subsequent JSON dump carries the canonical IDs.
            if workflow_definition is not None:
                await WorkflowParametersRepository._reconcile_definition_parameters_in_session(
                    session,
                    workflow_id,
                    list(workflow_definition.parameters),
                )
                # Propagate reconciled output-parameter IDs onto block-level
                # `output_parameter` references so the serialized JSON does
                # not depend on caller-side object identity between the
                # top-level parameters list and each block's field.
                _align_block_output_parameters(workflow_definition)
                workflow.workflow_definition = workflow_definition.model_dump(mode="json")
            if version is not None:
                workflow.version = version
            if run_with is not None:
                workflow.run_with = run_with
            if cache_key is not None:
                workflow.cache_key = cache_key
            if status is not None:
                workflow.status = status
            if import_error is not None:
                workflow.import_error = import_error
            if proxy_location is not _UNSET:
                workflow.proxy_location = serialize_proxy_location(cast(ProxyLocationInput, proxy_location))
            if webhook_callback_url is not _UNSET:
                workflow.webhook_callback_url = webhook_callback_url
            if persist_browser_session is not None:
                workflow.persist_browser_session = persist_browser_session
            if model is not _UNSET:
                workflow.model = model
            if max_screenshot_scrolling_times is not _UNSET:
                workflow.max_screenshot_scrolling_times = max_screenshot_scrolling_times
            if extra_http_headers is not _UNSET:
                workflow.extra_http_headers = extra_http_headers
            if ai_fallback is not None:
                workflow.ai_fallback = ai_fallback
            if run_sequentially is not None:
                workflow.run_sequentially = run_sequentially
            if sequential_key is not _UNSET:
                workflow.sequential_key = sequential_key

            await session.commit()
            await session.refresh(workflow)
            is_template = (
                await self.is_workflow_template(
                    workflow_permanent_id=workflow.workflow_permanent_id,
                    organization_id=workflow.organization_id,
                )
                if organization_id
                else False
            )
            return convert_to_workflow(
                workflow,
                self.debug_enabled,
                is_template=is_template,
            )

    @db_operation("soft_delete_workflow_and_schedules_by_permanent_id")
    async def soft_delete_workflow_and_schedules_by_permanent_id(
        self,
        workflow_permanent_id: str,
        organization_id: str | None = None,
    ) -> list[str]:
        """Soft-delete a workflow and its active schedules in a single DB transaction."""
        async with self.Session() as session:
            select_query = (
                select(WorkflowScheduleModel.workflow_schedule_id)
                .where(WorkflowScheduleModel.workflow_permanent_id == workflow_permanent_id)
                .where(WorkflowScheduleModel.deleted_at.is_(None))
            )
            if organization_id is not None:
                select_query = select_query.where(WorkflowScheduleModel.organization_id == organization_id)
            result = await session.execute(select_query)
            schedule_ids = list(result.scalars().all())

            deleted_at = datetime.now(timezone.utc)
            if schedule_ids:
                update_schedules_query = (
                    update(WorkflowScheduleModel)
                    .where(WorkflowScheduleModel.workflow_schedule_id.in_(schedule_ids))
                    .values(deleted_at=deleted_at)
                )
                await session.execute(update_schedules_query)

            update_workflow_query = (
                update(WorkflowModel)
                .where(WorkflowModel.workflow_permanent_id == workflow_permanent_id)
                .where(WorkflowModel.deleted_at.is_(None))
            )
            if organization_id is not None:
                update_workflow_query = update_workflow_query.filter_by(organization_id=organization_id)
            await session.execute(update_workflow_query.values(deleted_at=deleted_at))
            await session.commit()
            return schedule_ids

    @db_operation("add_workflow_template")
    async def add_workflow_template(
        self,
        workflow_permanent_id: str,
        organization_id: str,
    ) -> None:
        """Add a workflow to the templates table."""
        async with self.Session() as session:
            existing = (
                await session.scalars(
                    select(WorkflowTemplateModel)
                    .where(WorkflowTemplateModel.workflow_permanent_id == workflow_permanent_id)
                    .where(WorkflowTemplateModel.organization_id == organization_id)
                )
            ).first()
            if existing:
                if existing.deleted_at is not None:
                    existing.deleted_at = None
                    await session.commit()
                return
            template = WorkflowTemplateModel(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
            session.add(template)
            await session.commit()

    @db_operation("remove_workflow_template")
    async def remove_workflow_template(
        self,
        workflow_permanent_id: str,
        organization_id: str,
    ) -> None:
        """Soft delete a workflow from the templates table."""
        async with self.Session() as session:
            update_deleted_at_query = (
                update(WorkflowTemplateModel)
                .where(WorkflowTemplateModel.workflow_permanent_id == workflow_permanent_id)
                .where(WorkflowTemplateModel.organization_id == organization_id)
                .where(WorkflowTemplateModel.deleted_at.is_(None))
                .values(deleted_at=datetime.now(timezone.utc))
            )
            await session.execute(update_deleted_at_query)
            await session.commit()

    @db_operation("get_org_template_permanent_ids")
    async def get_org_template_permanent_ids(
        self,
        organization_id: str,
    ) -> set[str]:
        """Get all workflow_permanent_ids that are templates for an organization."""
        async with self.Session() as session:
            result = await session.scalars(
                select(WorkflowTemplateModel.workflow_permanent_id)
                .where(WorkflowTemplateModel.organization_id == organization_id)
                .where(WorkflowTemplateModel.deleted_at.is_(None))
            )
            return set(result.all())

    @db_operation("is_workflow_template")
    async def is_workflow_template(
        self,
        workflow_permanent_id: str,
        organization_id: str,
    ) -> bool:
        """Check if a workflow is marked as a template."""
        async with self.Session() as session:
            result = (
                await session.scalars(
                    select(WorkflowTemplateModel)
                    .where(WorkflowTemplateModel.workflow_permanent_id == workflow_permanent_id)
                    .where(WorkflowTemplateModel.organization_id == organization_id)
                    .where(WorkflowTemplateModel.deleted_at.is_(None))
                )
            ).first()
            return result is not None
