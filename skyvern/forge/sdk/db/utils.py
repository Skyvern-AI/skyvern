import json
import typing

import pydantic.json
import structlog

from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.models import (
    ActionModel,
    ArtifactModel,
    AWSSecretParameterModel,
    BitwardenLoginCredentialParameterModel,
    BitwardenSensitiveInformationParameterModel,
    OrganizationAuthTokenModel,
    OrganizationModel,
    OutputParameterModel,
    ScriptBlockModel,
    ScriptFileModel,
    ScriptModel,
    StepModel,
    TaskModel,
    TaskV2Model,
    WorkflowModel,
    WorkflowParameterModel,
    WorkflowRunBlockModel,
    WorkflowRunModel,
    WorkflowRunOutputParameterModel,
    WorkflowRunParameterModel,
)
from skyvern.forge.sdk.encrypt import encryptor
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.organizations import (
    AzureClientSecretCredential,
    AzureOrganizationAuthToken,
    Organization,
    OrganizationAuthToken,
)
from skyvern.forge.sdk.schemas.task_v2 import TaskV2
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.models.parameter import (
    AWSSecretParameter,
    BitwardenLoginCredentialParameter,
    BitwardenSensitiveInformationParameter,
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import (
    Workflow,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunOutputParameter,
    WorkflowRunParameter,
    WorkflowRunStatus,
    WorkflowStatus,
)
from skyvern.schemas.runs import GeoTarget, ProxyLocation, ProxyLocationInput, ScriptRunResponse
from skyvern.schemas.scripts import Script, ScriptBlock, ScriptFile
from skyvern.schemas.workflows import BlockStatus, BlockType
from skyvern.webeye.actions.actions import (
    Action,
    ActionType,
    CheckboxAction,
    ClickAction,
    CompleteAction,
    DownloadFileAction,
    DragAction,
    ExtractAction,
    GotoUrlAction,
    HoverAction,
    InputTextAction,
    KeypressAction,
    LeftMouseAction,
    MoveAction,
    NullAction,
    ReloadPageAction,
    ScrollAction,
    SelectOptionAction,
    SolveCaptchaAction,
    TerminateAction,
    UploadFileAction,
    VerificationCodeAction,
    WaitAction,
)

LOG = structlog.get_logger()


def _deserialize_proxy_location(value: str | None) -> ProxyLocationInput:
    """
    Deserialize proxy_location from database storage.

    Handles:
    - None -> None
    - ProxyLocation enum string (e.g., "RESIDENTIAL") -> ProxyLocation enum
    - JSON string (e.g., '{"country": "US", ...}') -> GeoTarget object
    """
    if value is None:
        return None

    result: ProxyLocationInput = None

    # Try to parse as JSON first (for GeoTarget)
    if value.startswith("{"):
        try:
            data = json.loads(value)
            result = GeoTarget.model_validate(data)
            LOG.info(
                "Deserialized proxy_location as GeoTarget",
                db_value=value,
                result=str(result),
            )
            return result
        except (json.JSONDecodeError, ValueError):
            pass

    # Try as ProxyLocation enum
    try:
        result = ProxyLocation(value)
        return result
    except ValueError:
        # If all else fails, return as-is (shouldn't happen with valid data)
        LOG.warning("Failed to deserialize proxy_location", db_value=value)
        return None


# Mapping of action types to their corresponding action classes
ACTION_TYPE_TO_CLASS = {
    ActionType.CLICK: ClickAction,
    ActionType.INPUT_TEXT: InputTextAction,
    ActionType.UPLOAD_FILE: UploadFileAction,
    ActionType.DOWNLOAD_FILE: DownloadFileAction,
    ActionType.NULL_ACTION: NullAction,
    ActionType.TERMINATE: TerminateAction,
    ActionType.COMPLETE: CompleteAction,
    ActionType.SELECT_OPTION: SelectOptionAction,
    ActionType.CHECKBOX: CheckboxAction,
    ActionType.WAIT: WaitAction,
    ActionType.HOVER: HoverAction,
    ActionType.SOLVE_CAPTCHA: SolveCaptchaAction,
    ActionType.RELOAD_PAGE: ReloadPageAction,
    ActionType.EXTRACT: ExtractAction,
    ActionType.SCROLL: ScrollAction,
    ActionType.KEYPRESS: KeypressAction,
    ActionType.MOVE: MoveAction,
    ActionType.DRAG: DragAction,
    ActionType.VERIFICATION_CODE: VerificationCodeAction,
    ActionType.LEFT_MOUSE: LeftMouseAction,
    ActionType.GOTO_URL: GotoUrlAction,
}


@typing.no_type_check
def _custom_json_serializer(*args, **kwargs) -> str:
    """
    Encodes json in the same way that pydantic does.
    """
    return json.dumps(*args, default=pydantic.json.pydantic_encoder, **kwargs)


def convert_to_task(task_obj: TaskModel, debug_enabled: bool = False, workflow_permanent_id: str | None = None) -> Task:
    if debug_enabled:
        LOG.debug("Converting TaskModel to Task", task_id=task_obj.task_id)
    task = Task(
        task_id=task_obj.task_id,
        status=TaskStatus(task_obj.status),
        created_at=task_obj.created_at,
        modified_at=task_obj.modified_at,
        task_type=task_obj.task_type,
        title=task_obj.title,
        url=task_obj.url,
        complete_criterion=task_obj.complete_criterion,
        terminate_criterion=task_obj.terminate_criterion,
        include_action_history_in_verification=task_obj.include_action_history_in_verification,
        webhook_callback_url=task_obj.webhook_callback_url,
        webhook_failure_reason=task_obj.webhook_failure_reason,
        totp_verification_url=task_obj.totp_verification_url,
        totp_identifier=task_obj.totp_identifier,
        navigation_goal=task_obj.navigation_goal,
        data_extraction_goal=task_obj.data_extraction_goal,
        navigation_payload=task_obj.navigation_payload,
        extracted_information=task_obj.extracted_information,
        failure_reason=task_obj.failure_reason,
        organization_id=task_obj.organization_id,
        proxy_location=_deserialize_proxy_location(task_obj.proxy_location),
        extracted_information_schema=task_obj.extracted_information_schema,
        extra_http_headers=task_obj.extra_http_headers,
        workflow_run_id=task_obj.workflow_run_id,
        workflow_permanent_id=workflow_permanent_id,
        order=task_obj.order,
        retry=task_obj.retry,
        max_steps_per_run=task_obj.max_steps_per_run,
        error_code_mapping=task_obj.error_code_mapping,
        errors=task_obj.errors,
        application=task_obj.application,
        model=task_obj.model,
        queued_at=task_obj.queued_at,
        started_at=task_obj.started_at,
        finished_at=task_obj.finished_at,
        max_screenshot_scrolls=task_obj.max_screenshot_scrolling_times,
        browser_session_id=task_obj.browser_session_id,
        browser_address=task_obj.browser_address,
        download_timeout=task_obj.download_timeout,
    )
    return task


def convert_to_task_v2(task_v2_model: TaskV2Model, debug_enabled: bool = False) -> TaskV2:
    if debug_enabled:
        LOG.debug("Converting TaskV2Model to TaskV2", observer_cruise_id=task_v2_model.observer_cruise_id)
    task_v2_data = {column.name: getattr(task_v2_model, column.name) for column in TaskV2Model.__table__.columns}
    #  Deserialize proxy_location FIRST (string → GeoTarget), otherwise model_validate will fail for city/state proxy selections
    task_v2_data["proxy_location"] = _deserialize_proxy_location(task_v2_model.proxy_location)
    return TaskV2.model_validate(task_v2_data)


def convert_to_step(step_model: StepModel, debug_enabled: bool = False) -> Step:
    if debug_enabled:
        LOG.debug("Converting StepModel to Step", step_id=step_model.step_id)
    return Step(
        task_id=step_model.task_id,
        step_id=step_model.step_id,
        created_at=step_model.created_at,
        modified_at=step_model.modified_at,
        status=StepStatus(step_model.status),
        output=step_model.output,
        order=step_model.order,
        is_last=step_model.is_last,
        retry_index=step_model.retry_index,
        organization_id=step_model.organization_id,
        input_token_count=step_model.input_token_count,
        output_token_count=step_model.output_token_count,
        reasoning_token_count=step_model.reasoning_token_count,
        cached_token_count=step_model.cached_token_count,
        step_cost=step_model.step_cost,
        created_by=step_model.created_by,
    )


def convert_to_organization(org_model: OrganizationModel) -> Organization:
    return Organization(
        organization_id=org_model.organization_id,
        organization_name=org_model.organization_name,
        webhook_callback_url=org_model.webhook_callback_url,
        max_steps_per_run=org_model.max_steps_per_run,
        max_retries_per_step=org_model.max_retries_per_step,
        domain=org_model.domain,
        bw_organization_id=org_model.bw_organization_id,
        bw_collection_ids=org_model.bw_collection_ids,
        created_at=org_model.created_at,
        modified_at=org_model.modified_at,
    )


async def convert_to_organization_auth_token(
    org_auth_token: OrganizationAuthTokenModel, token_type: str
) -> OrganizationAuthToken | AzureOrganizationAuthToken:
    token = org_auth_token.token
    if org_auth_token.encrypted_token and org_auth_token.encrypted_method:
        token = await encryptor.decrypt(org_auth_token.encrypted_token, EncryptMethod(org_auth_token.encrypted_method))

    if token_type == OrganizationAuthTokenType.azure_client_secret_credential:
        credential = AzureClientSecretCredential.model_validate_json(token)
        return AzureOrganizationAuthToken(
            id=org_auth_token.id,
            organization_id=org_auth_token.organization_id,
            token_type=OrganizationAuthTokenType(org_auth_token.token_type),
            credential=credential,
            valid=org_auth_token.valid,
            created_at=org_auth_token.created_at,
            modified_at=org_auth_token.modified_at,
        )
    else:
        return OrganizationAuthToken(
            id=org_auth_token.id,
            organization_id=org_auth_token.organization_id,
            token_type=OrganizationAuthTokenType(org_auth_token.token_type),
            token=token,
            valid=org_auth_token.valid,
            created_at=org_auth_token.created_at,
            modified_at=org_auth_token.modified_at,
        )


def convert_to_artifact(artifact_model: ArtifactModel, debug_enabled: bool = False) -> Artifact:
    if debug_enabled:
        LOG.debug(
            "Converting ArtifactModel to Artifact",
            artifact_id=artifact_model.artifact_id,
        )

    return Artifact(
        artifact_id=artifact_model.artifact_id,
        artifact_type=ArtifactType[artifact_model.artifact_type.upper()],
        uri=artifact_model.uri,
        task_id=artifact_model.task_id,
        step_id=artifact_model.step_id,
        workflow_run_id=artifact_model.workflow_run_id,
        workflow_run_block_id=artifact_model.workflow_run_block_id,
        run_id=artifact_model.run_id,
        observer_cruise_id=artifact_model.observer_cruise_id,
        observer_thought_id=artifact_model.observer_thought_id,
        created_at=artifact_model.created_at,
        modified_at=artifact_model.modified_at,
        organization_id=artifact_model.organization_id,
    )


def convert_to_workflow(
    workflow_model: WorkflowModel,
    debug_enabled: bool = False,
    is_template: bool = False,
) -> Workflow:
    """
    Convert a WorkflowModel to a Workflow pydantic model.

    Args:
        workflow_model: The database model to convert.
        debug_enabled: Whether to log debug messages.
        is_template: Whether this workflow is marked as a template.
            This is computed separately from the workflow_templates table
            since template status is at the workflow_permanent_id level,
            not the versioned workflow level.
    """
    if debug_enabled:
        LOG.debug(
            "Converting WorkflowModel to Workflow",
            workflow_id=workflow_model.workflow_id,
        )

    return Workflow(
        workflow_id=workflow_model.workflow_id,
        organization_id=workflow_model.organization_id,
        title=workflow_model.title,
        workflow_permanent_id=workflow_model.workflow_permanent_id,
        webhook_callback_url=workflow_model.webhook_callback_url,
        totp_verification_url=workflow_model.totp_verification_url,
        totp_identifier=workflow_model.totp_identifier,
        persist_browser_session=workflow_model.persist_browser_session,
        model=workflow_model.model,
        proxy_location=_deserialize_proxy_location(workflow_model.proxy_location),
        max_screenshot_scrolls=workflow_model.max_screenshot_scrolling_times,
        version=workflow_model.version,
        is_saved_task=workflow_model.is_saved_task,
        is_template=is_template,
        description=workflow_model.description,
        workflow_definition=WorkflowDefinition.model_validate(workflow_model.workflow_definition),
        created_at=workflow_model.created_at,
        modified_at=workflow_model.modified_at,
        deleted_at=workflow_model.deleted_at,
        status=WorkflowStatus(workflow_model.status),
        extra_http_headers=workflow_model.extra_http_headers,
        run_with=workflow_model.run_with,
        ai_fallback=workflow_model.ai_fallback,
        cache_key=workflow_model.cache_key,
        run_sequentially=workflow_model.run_sequentially,
        sequential_key=workflow_model.sequential_key,
        folder_id=workflow_model.folder_id,
        import_error=workflow_model.import_error,
    )


def convert_to_workflow_run(
    workflow_run_model: WorkflowRunModel, workflow_title: str | None = None, debug_enabled: bool = False
) -> WorkflowRun:
    if debug_enabled:
        LOG.debug(
            "Converting WorkflowRunModel to WorkflowRun",
            workflow_run_id=workflow_run_model.workflow_run_id,
        )

    return WorkflowRun(
        workflow_run_id=workflow_run_model.workflow_run_id,
        workflow_permanent_id=workflow_run_model.workflow_permanent_id,
        parent_workflow_run_id=workflow_run_model.parent_workflow_run_id,
        workflow_id=workflow_run_model.workflow_id,
        organization_id=workflow_run_model.organization_id,
        browser_session_id=workflow_run_model.browser_session_id,
        debug_session_id=workflow_run_model.debug_session_id,
        browser_profile_id=workflow_run_model.browser_profile_id,
        status=WorkflowRunStatus[workflow_run_model.status],
        failure_reason=workflow_run_model.failure_reason,
        proxy_location=_deserialize_proxy_location(workflow_run_model.proxy_location),
        webhook_callback_url=workflow_run_model.webhook_callback_url,
        webhook_failure_reason=workflow_run_model.webhook_failure_reason,
        totp_verification_url=workflow_run_model.totp_verification_url,
        totp_identifier=workflow_run_model.totp_identifier,
        queued_at=workflow_run_model.queued_at,
        started_at=workflow_run_model.started_at,
        finished_at=workflow_run_model.finished_at,
        created_at=workflow_run_model.created_at,
        modified_at=workflow_run_model.modified_at,
        workflow_title=workflow_title,
        max_screenshot_scrolls=workflow_run_model.max_screenshot_scrolling_times,
        extra_http_headers=workflow_run_model.extra_http_headers,
        browser_address=workflow_run_model.browser_address,
        job_id=workflow_run_model.job_id,
        depends_on_workflow_run_id=workflow_run_model.depends_on_workflow_run_id,
        sequential_key=workflow_run_model.sequential_key,
        script_run=ScriptRunResponse.model_validate(workflow_run_model.script_run)
        if workflow_run_model.script_run
        else None,
        run_with=workflow_run_model.run_with,
        code_gen=workflow_run_model.code_gen,
    )


def convert_to_workflow_parameter(
    workflow_parameter_model: WorkflowParameterModel, debug_enabled: bool = False
) -> WorkflowParameter:
    if debug_enabled:
        LOG.debug(
            "Converting WorkflowParameterModel to WorkflowParameter",
            workflow_parameter_id=workflow_parameter_model.workflow_parameter_id,
        )

    workflow_parameter_type = WorkflowParameterType[workflow_parameter_model.workflow_parameter_type.upper()]

    return WorkflowParameter(
        workflow_parameter_id=workflow_parameter_model.workflow_parameter_id,
        workflow_parameter_type=workflow_parameter_type,
        workflow_id=workflow_parameter_model.workflow_id,
        default_value=workflow_parameter_type.convert_value(workflow_parameter_model.default_value),
        key=workflow_parameter_model.key,
        description=workflow_parameter_model.description,
        created_at=workflow_parameter_model.created_at,
        modified_at=workflow_parameter_model.modified_at,
        deleted_at=workflow_parameter_model.deleted_at,
    )


def convert_to_aws_secret_parameter(
    aws_secret_parameter_model: AWSSecretParameterModel, debug_enabled: bool = False
) -> AWSSecretParameter:
    if debug_enabled:
        LOG.debug(
            "Converting AWSSecretParameterModel to AWSSecretParameter",
            aws_secret_parameter_id=aws_secret_parameter_model.aws_secret_parameter_id,
        )

    return AWSSecretParameter(
        aws_secret_parameter_id=aws_secret_parameter_model.aws_secret_parameter_id,
        workflow_id=aws_secret_parameter_model.workflow_id,
        key=aws_secret_parameter_model.key,
        description=aws_secret_parameter_model.description,
        aws_key=aws_secret_parameter_model.aws_key,
        created_at=aws_secret_parameter_model.created_at,
        modified_at=aws_secret_parameter_model.modified_at,
        deleted_at=aws_secret_parameter_model.deleted_at,
    )


def convert_to_bitwarden_login_credential_parameter(
    bitwarden_login_credential_parameter_model: BitwardenLoginCredentialParameterModel,
    debug_enabled: bool = False,
) -> BitwardenLoginCredentialParameter:
    if debug_enabled:
        LOG.debug(
            "Converting BitwardenLoginCredentialParameterModel to BitwardenLoginCredentialParameter",
            bitwarden_login_credential_parameter_id=bitwarden_login_credential_parameter_model.bitwarden_login_credential_parameter_id,
            bitwarden_collection_id=bitwarden_login_credential_parameter_model.bitwarden_collection_id,
        )

    return BitwardenLoginCredentialParameter(
        bitwarden_login_credential_parameter_id=bitwarden_login_credential_parameter_model.bitwarden_login_credential_parameter_id,
        workflow_id=bitwarden_login_credential_parameter_model.workflow_id,
        key=bitwarden_login_credential_parameter_model.key,
        description=bitwarden_login_credential_parameter_model.description,
        bitwarden_client_id_aws_secret_key=bitwarden_login_credential_parameter_model.bitwarden_client_id_aws_secret_key,
        bitwarden_client_secret_aws_secret_key=bitwarden_login_credential_parameter_model.bitwarden_client_secret_aws_secret_key,
        bitwarden_master_password_aws_secret_key=bitwarden_login_credential_parameter_model.bitwarden_master_password_aws_secret_key,
        bitwarden_collection_id=bitwarden_login_credential_parameter_model.bitwarden_collection_id,
        bitwarden_item_id=bitwarden_login_credential_parameter_model.bitwarden_item_id,
        url_parameter_key=bitwarden_login_credential_parameter_model.url_parameter_key,
        created_at=bitwarden_login_credential_parameter_model.created_at,
        modified_at=bitwarden_login_credential_parameter_model.modified_at,
        deleted_at=bitwarden_login_credential_parameter_model.deleted_at,
    )


def convert_to_bitwarden_sensitive_information_parameter(
    bitwarden_sensitive_information_parameter_model: BitwardenSensitiveInformationParameterModel,
    debug_enabled: bool = False,
) -> BitwardenSensitiveInformationParameter:
    if debug_enabled:
        LOG.debug(
            "Converting BitwardenSensitiveInformationParameterModel to BitwardenSensitiveInformationParameter",
            bitwarden_sensitive_information_parameter_id=bitwarden_sensitive_information_parameter_model.bitwarden_sensitive_information_parameter_id,
        )

    return BitwardenSensitiveInformationParameter(
        bitwarden_sensitive_information_parameter_id=bitwarden_sensitive_information_parameter_model.bitwarden_sensitive_information_parameter_id,
        workflow_id=bitwarden_sensitive_information_parameter_model.workflow_id,
        key=bitwarden_sensitive_information_parameter_model.key,
        description=bitwarden_sensitive_information_parameter_model.description,
        bitwarden_client_id_aws_secret_key=bitwarden_sensitive_information_parameter_model.bitwarden_client_id_aws_secret_key,
        bitwarden_client_secret_aws_secret_key=bitwarden_sensitive_information_parameter_model.bitwarden_client_secret_aws_secret_key,
        bitwarden_master_password_aws_secret_key=bitwarden_sensitive_information_parameter_model.bitwarden_master_password_aws_secret_key,
        bitwarden_collection_id=bitwarden_sensitive_information_parameter_model.bitwarden_collection_id,
        bitwarden_identity_key=bitwarden_sensitive_information_parameter_model.bitwarden_identity_key,
        bitwarden_identity_fields=bitwarden_sensitive_information_parameter_model.bitwarden_identity_fields,
        created_at=bitwarden_sensitive_information_parameter_model.created_at,
        modified_at=bitwarden_sensitive_information_parameter_model.modified_at,
        deleted_at=bitwarden_sensitive_information_parameter_model.deleted_at,
    )


def convert_to_output_parameter(
    output_parameter_model: OutputParameterModel, debug_enabled: bool = False
) -> OutputParameter:
    if debug_enabled:
        LOG.debug(
            "Converting OutputParameterModel to OutputParameter",
            output_parameter_id=output_parameter_model.output_parameter_id,
        )

    return OutputParameter(
        output_parameter_id=output_parameter_model.output_parameter_id,
        key=output_parameter_model.key,
        description=output_parameter_model.description,
        workflow_id=output_parameter_model.workflow_id,
        created_at=output_parameter_model.created_at,
        modified_at=output_parameter_model.modified_at,
        deleted_at=output_parameter_model.deleted_at,
    )


def convert_to_workflow_run_output_parameter(
    workflow_run_output_parameter_model: WorkflowRunOutputParameterModel,
    debug_enabled: bool = False,
) -> WorkflowRunOutputParameter:
    if debug_enabled:
        LOG.debug(
            "Converting WorkflowRunOutputParameterModel to WorkflowRunOutputParameter",
            workflow_run_id=workflow_run_output_parameter_model.workflow_run_id,
            output_parameter_id=workflow_run_output_parameter_model.output_parameter_id,
        )

    return WorkflowRunOutputParameter(
        workflow_run_id=workflow_run_output_parameter_model.workflow_run_id,
        output_parameter_id=workflow_run_output_parameter_model.output_parameter_id,
        value=workflow_run_output_parameter_model.value,
        created_at=workflow_run_output_parameter_model.created_at,
    )


def convert_to_workflow_run_parameter(
    workflow_run_parameter_model: WorkflowRunParameterModel,
    workflow_parameter: WorkflowParameter,
    debug_enabled: bool = False,
) -> WorkflowRunParameter:
    if debug_enabled:
        LOG.debug(
            "Converting WorkflowRunParameterModel to WorkflowRunParameter",
            workflow_run_id=workflow_run_parameter_model.workflow_run_id,
            workflow_parameter_id=workflow_run_parameter_model.workflow_parameter_id,
        )

    return WorkflowRunParameter(
        workflow_run_id=workflow_run_parameter_model.workflow_run_id,
        workflow_parameter_id=workflow_run_parameter_model.workflow_parameter_id,
        value=workflow_parameter.workflow_parameter_type.convert_value(workflow_run_parameter_model.value),
        created_at=workflow_run_parameter_model.created_at,
    )


def convert_to_workflow_run_block(
    workflow_run_block_model: WorkflowRunBlockModel,
    task: Task | None = None,
) -> WorkflowRunBlock:
    block = WorkflowRunBlock(
        workflow_run_block_id=workflow_run_block_model.workflow_run_block_id,
        workflow_run_id=workflow_run_block_model.workflow_run_id,
        block_workflow_run_id=workflow_run_block_model.block_workflow_run_id,
        organization_id=workflow_run_block_model.organization_id,
        parent_workflow_run_block_id=workflow_run_block_model.parent_workflow_run_block_id,
        description=workflow_run_block_model.description,
        block_type=BlockType(workflow_run_block_model.block_type),
        label=workflow_run_block_model.label,
        status=BlockStatus(workflow_run_block_model.status),
        output=workflow_run_block_model.output,
        continue_on_failure=workflow_run_block_model.continue_on_failure,
        failure_reason=workflow_run_block_model.failure_reason,
        engine=workflow_run_block_model.engine,
        task_id=workflow_run_block_model.task_id,
        loop_values=workflow_run_block_model.loop_values,
        current_value=workflow_run_block_model.current_value,
        current_index=workflow_run_block_model.current_index,
        recipients=workflow_run_block_model.recipients,
        attachments=workflow_run_block_model.attachments,
        subject=workflow_run_block_model.subject,
        body=workflow_run_block_model.body,
        created_at=workflow_run_block_model.created_at,
        modified_at=workflow_run_block_model.modified_at,
        instructions=workflow_run_block_model.instructions,
        positive_descriptor=workflow_run_block_model.positive_descriptor,
        negative_descriptor=workflow_run_block_model.negative_descriptor,
        executed_branch_id=workflow_run_block_model.executed_branch_id,
        executed_branch_expression=workflow_run_block_model.executed_branch_expression,
        executed_branch_result=workflow_run_block_model.executed_branch_result,
        executed_branch_next_block=workflow_run_block_model.executed_branch_next_block,
    )
    if task:
        if task.finished_at and task.started_at:
            duration = task.finished_at - task.started_at
            block.duration = duration.total_seconds()
        block.url = task.url
        block.navigation_goal = task.navigation_goal
        block.navigation_payload = task.navigation_payload
        block.data_extraction_goal = task.data_extraction_goal
        block.data_schema = task.extracted_information_schema
        block.terminate_criterion = task.terminate_criterion
        block.complete_criterion = task.complete_criterion
        block.include_action_history_in_verification = task.include_action_history_in_verification

    return block


def convert_to_script(script_model: ScriptModel) -> Script:
    return Script(
        script_revision_id=script_model.script_revision_id,
        script_id=script_model.script_id,
        organization_id=script_model.organization_id,
        run_id=script_model.run_id,
        version=script_model.version,
        created_at=script_model.created_at,
        modified_at=script_model.modified_at,
        deleted_at=script_model.deleted_at,
    )


def convert_to_script_file(script_file_model: ScriptFileModel) -> ScriptFile:
    return ScriptFile(
        file_id=script_file_model.file_id,
        script_revision_id=script_file_model.script_revision_id,
        script_id=script_file_model.script_id,
        organization_id=script_file_model.organization_id,
        file_path=script_file_model.file_path,
        file_name=script_file_model.file_name,
        file_type=script_file_model.file_type,
        content_hash=script_file_model.content_hash,
        file_size=script_file_model.file_size,
        mime_type=script_file_model.mime_type,
        encoding=script_file_model.encoding,
        artifact_id=script_file_model.artifact_id,
        created_at=script_file_model.created_at,
        modified_at=script_file_model.modified_at,
    )


def convert_to_script_block(script_block_model: ScriptBlockModel) -> ScriptBlock:
    return ScriptBlock(
        script_block_id=script_block_model.script_block_id,
        organization_id=script_block_model.organization_id,
        script_id=script_block_model.script_id,
        script_revision_id=script_block_model.script_revision_id,
        script_block_label=script_block_model.script_block_label,
        script_file_id=script_block_model.script_file_id,
        run_signature=script_block_model.run_signature,
        workflow_run_id=script_block_model.workflow_run_id,
        workflow_run_block_id=script_block_model.workflow_run_block_id,
        input_fields=script_block_model.input_fields,
        created_at=script_block_model.created_at,
        modified_at=script_block_model.modified_at,
        deleted_at=script_block_model.deleted_at,
    )


def hydrate_action(action_model: ActionModel, empty_element_id: bool = False) -> Action:
    """
    Convert ActionModel to the appropriate Action type based on action_type.
    The action_json contains all the metadata of different types of actions.
    """
    # Create base action data from the model
    element_id = action_model.element_id
    if empty_element_id:
        element_id = element_id or ""
    action_data = {
        "action_type": action_model.action_type,
        "status": action_model.status,
        "action_id": action_model.action_id,
        "source_action_id": action_model.source_action_id,
        "organization_id": action_model.organization_id,
        "workflow_run_id": action_model.workflow_run_id,
        "task_id": action_model.task_id,
        "step_id": action_model.step_id,
        "step_order": action_model.step_order,
        "action_order": action_model.action_order,
        "confidence_float": action_model.confidence_float,
        "reasoning": action_model.reasoning,
        "intention": action_model.intention,
        "response": action_model.response,
        "element_id": element_id,
        "skyvern_element_hash": action_model.skyvern_element_hash,
        "skyvern_element_data": action_model.skyvern_element_data,
        "created_at": action_model.created_at,
        "modified_at": action_model.modified_at,
    }

    # Merge with action_json data, skipping None values
    if action_model.action_json:
        for key, value in action_model.action_json.items():
            if value is not None:
                action_data[key] = value

    # Get the appropriate action class and instantiate it
    action_class = ACTION_TYPE_TO_CLASS.get(action_model.action_type)
    if action_class is None:
        raise ValueError(f"Unsupported action type: {action_model.action_type}")

    return action_class(**action_data)
