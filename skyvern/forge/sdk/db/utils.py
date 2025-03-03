import json
import typing

import pydantic.json
import structlog

from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.models import (
    ArtifactModel,
    AWSSecretParameterModel,
    BitwardenLoginCredentialParameterModel,
    BitwardenSensitiveInformationParameterModel,
    OrganizationAuthTokenModel,
    OrganizationModel,
    OutputParameterModel,
    StepModel,
    TaskModel,
    WorkflowModel,
    WorkflowParameterModel,
    WorkflowRunBlockModel,
    WorkflowRunModel,
    WorkflowRunOutputParameterModel,
    WorkflowRunParameterModel,
)
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.organizations import Organization, OrganizationAuthToken
from skyvern.forge.sdk.schemas.tasks import ProxyLocation, Task, TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.models.block import BlockStatus, BlockType
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

LOG = structlog.get_logger()


@typing.no_type_check
def _custom_json_serializer(*args, **kwargs) -> str:
    """
    Encodes json in the same way that pydantic does.
    """
    return json.dumps(*args, default=pydantic.json.pydantic_encoder, **kwargs)


def convert_to_task(task_obj: TaskModel, debug_enabled: bool = False) -> Task:
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
        webhook_callback_url=task_obj.webhook_callback_url,
        totp_verification_url=task_obj.totp_verification_url,
        totp_identifier=task_obj.totp_identifier,
        navigation_goal=task_obj.navigation_goal,
        data_extraction_goal=task_obj.data_extraction_goal,
        navigation_payload=task_obj.navigation_payload,
        extracted_information=task_obj.extracted_information,
        failure_reason=task_obj.failure_reason,
        organization_id=task_obj.organization_id,
        proxy_location=(ProxyLocation(task_obj.proxy_location) if task_obj.proxy_location else None),
        extracted_information_schema=task_obj.extracted_information_schema,
        workflow_run_id=task_obj.workflow_run_id,
        order=task_obj.order,
        retry=task_obj.retry,
        max_steps_per_run=task_obj.max_steps_per_run,
        error_code_mapping=task_obj.error_code_mapping,
        errors=task_obj.errors,
        application=task_obj.application,
    )
    return task


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
        step_cost=step_model.step_cost,
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


def convert_to_organization_auth_token(
    org_auth_token: OrganizationAuthTokenModel,
) -> OrganizationAuthToken:
    return OrganizationAuthToken(
        id=org_auth_token.id,
        organization_id=org_auth_token.organization_id,
        token_type=OrganizationAuthTokenType(org_auth_token.token_type),
        token=org_auth_token.token,
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
        observer_cruise_id=artifact_model.observer_cruise_id,
        observer_thought_id=artifact_model.observer_thought_id,
        created_at=artifact_model.created_at,
        modified_at=artifact_model.modified_at,
        organization_id=artifact_model.organization_id,
    )


def convert_to_workflow(workflow_model: WorkflowModel, debug_enabled: bool = False) -> Workflow:
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
        proxy_location=(ProxyLocation(workflow_model.proxy_location) if workflow_model.proxy_location else None),
        version=workflow_model.version,
        is_saved_task=workflow_model.is_saved_task,
        description=workflow_model.description,
        workflow_definition=WorkflowDefinition.model_validate(workflow_model.workflow_definition),
        created_at=workflow_model.created_at,
        modified_at=workflow_model.modified_at,
        deleted_at=workflow_model.deleted_at,
        status=WorkflowStatus(workflow_model.status),
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
        status=WorkflowRunStatus[workflow_run_model.status],
        failure_reason=workflow_run_model.failure_reason,
        proxy_location=(
            ProxyLocation(workflow_run_model.proxy_location) if workflow_run_model.proxy_location else None
        ),
        webhook_callback_url=workflow_run_model.webhook_callback_url,
        totp_verification_url=workflow_run_model.totp_verification_url,
        totp_identifier=workflow_run_model.totp_identifier,
        created_at=workflow_run_model.created_at,
        modified_at=workflow_run_model.modified_at,
        workflow_title=workflow_title,
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
    )
    if task:
        block.url = task.url
        block.navigation_goal = task.navigation_goal
        block.navigation_payload = task.navigation_payload
        block.data_extraction_goal = task.data_extraction_goal
        block.data_schema = task.extracted_information_schema
        block.terminate_criterion = task.terminate_criterion
        block.complete_criterion = task.complete_criterion

    return block
