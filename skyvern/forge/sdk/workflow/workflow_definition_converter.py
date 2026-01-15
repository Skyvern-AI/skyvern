import json
from datetime import UTC, datetime
from typing import Any, cast

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.db.id import (
    generate_aws_secret_parameter_id,
    generate_azure_vault_credential_parameter_id,
    generate_bitwarden_credit_card_data_parameter_id,
    generate_bitwarden_login_credential_parameter_id,
    generate_bitwarden_sensitive_information_parameter_id,
    generate_credential_parameter_id,
    generate_onepassword_credential_parameter_id,
    generate_output_parameter_id,
    generate_workflow_parameter_id,
)
from skyvern.forge.sdk.workflow.exceptions import (
    ContextParameterSourceNotDefined,
    InvalidWaitBlockTime,
    InvalidWorkflowDefinition,
    WorkflowDefinitionHasDuplicateParameterKeys,
    WorkflowDefinitionHasReservedParameterKeys,
    WorkflowDefinitionHasUndefinedParameters,
    WorkflowParameterMissingRequiredValue,
)
from skyvern.forge.sdk.workflow.models.block import (
    ActionBlock,
    BlockTypeVar,
    BranchCondition,
    CodeBlock,
    ConditionalBlock,
    DownloadToS3Block,
    ExtractionBlock,
    FileDownloadBlock,
    FileParserBlock,
    FileUploadBlock,
    ForLoopBlock,
    HttpRequestBlock,
    HumanInteractionBlock,
    JinjaBranchCriteria,
    LoginBlock,
    NavigationBlock,
    PDFParserBlock,
    PrintPageBlock,
    PromptBranchCriteria,
    SendEmailBlock,
    TaskBlock,
    TaskV2Block,
    TextPromptBlock,
    UploadToS3Block,
    UrlBlock,
    ValidationBlock,
    WaitBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    PARAMETER_TYPE,
    RESERVED_PARAMETER_KEYS,
    AWSSecretParameter,
    AzureVaultCredentialParameter,
    BitwardenCreditCardDataParameter,
    BitwardenLoginCredentialParameter,
    BitwardenSensitiveInformationParameter,
    ContextParameter,
    CredentialParameter,
    OnePasswordCredentialParameter,
    OutputParameter,
    Parameter,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import (
    WorkflowDefinition,
)
from skyvern.schemas.workflows import (
    BLOCK_YAML_TYPES,
    BlockType,
    ForLoopBlockYAML,
    WorkflowDefinitionYAML,
)

LOG = structlog.get_logger()


def convert_workflow_definition(
    workflow_definition_yaml: WorkflowDefinitionYAML,
    workflow_id: str,
) -> WorkflowDefinition:
    # Create parameters from the request
    parameters: dict[str, PARAMETER_TYPE] = {}
    duplicate_parameter_keys = set()

    # Check if user's trying to manually create an output parameter
    if any(parameter.parameter_type == ParameterType.OUTPUT for parameter in workflow_definition_yaml.parameters):
        raise InvalidWorkflowDefinition(message="Cannot manually create output parameters")

    # Check if any parameter keys collide with automatically created output parameter keys
    block_labels = [block.label for block in workflow_definition_yaml.blocks]
    # TODO (kerem): Check if block labels are unique
    output_parameter_keys = [f"{block_label}_output" for block_label in block_labels]
    parameter_keys = [parameter.key for parameter in workflow_definition_yaml.parameters]
    if any(key in output_parameter_keys for key in parameter_keys):
        raise WorkflowDefinitionHasReservedParameterKeys(
            reserved_keys=output_parameter_keys, parameter_keys=parameter_keys
        )

    if any(key in RESERVED_PARAMETER_KEYS for key in parameter_keys):
        raise WorkflowDefinitionHasReservedParameterKeys(
            reserved_keys=RESERVED_PARAMETER_KEYS,
            parameter_keys=parameter_keys,
        )

    # Create output parameters for all blocks
    block_output_parameters = _create_all_output_parameters_for_workflow(
        workflow_id=workflow_id,
        block_yamls=workflow_definition_yaml.blocks,
    )
    for block_output_parameter in block_output_parameters.values():
        parameters[block_output_parameter.key] = block_output_parameter

    # We're going to process context parameters after other parameters since they depend on the other parameters
    context_parameter_yamls = []

    for parameter in workflow_definition_yaml.parameters:
        if parameter.key in parameters:
            LOG.error(f"Duplicate parameter key {parameter.key}")
            duplicate_parameter_keys.add(parameter.key)
            continue
        now = datetime.now(UTC)
        if parameter.parameter_type == ParameterType.AWS_SECRET:
            parameters[parameter.key] = AWSSecretParameter(
                aws_secret_parameter_id=generate_aws_secret_parameter_id(),
                workflow_id=workflow_id,
                aws_key=parameter.aws_key,
                key=parameter.key,
                description=parameter.description,
                created_at=now,
                modified_at=now,
            )
        elif parameter.parameter_type == ParameterType.CREDENTIAL:
            parameters[parameter.key] = CredentialParameter(
                credential_parameter_id=generate_credential_parameter_id(),
                workflow_id=workflow_id,
                key=parameter.key,
                description=parameter.description,
                credential_id=parameter.credential_id,
                created_at=now,
                modified_at=now,
            )
        elif parameter.parameter_type == ParameterType.ONEPASSWORD:
            parameters[parameter.key] = OnePasswordCredentialParameter(
                onepassword_credential_parameter_id=generate_onepassword_credential_parameter_id(),
                workflow_id=workflow_id,
                key=parameter.key,
                description=parameter.description,
                vault_id=parameter.vault_id,
                item_id=parameter.item_id,
                created_at=now,
                modified_at=now,
            )
        elif parameter.parameter_type == ParameterType.AZURE_VAULT_CREDENTIAL:
            parameters[parameter.key] = AzureVaultCredentialParameter(
                azure_vault_credential_parameter_id=generate_azure_vault_credential_parameter_id(),
                workflow_id=workflow_id,
                key=parameter.key,
                description=parameter.description,
                vault_name=parameter.vault_name,
                username_key=parameter.username_key,
                password_key=parameter.password_key,
                totp_secret_key=parameter.totp_secret_key,
                created_at=now,
                modified_at=now,
            )
        elif parameter.parameter_type == ParameterType.BITWARDEN_LOGIN_CREDENTIAL:
            if not parameter.bitwarden_collection_id and not parameter.bitwarden_item_id:
                raise WorkflowParameterMissingRequiredValue(
                    workflow_parameter_type=ParameterType.BITWARDEN_LOGIN_CREDENTIAL,
                    workflow_parameter_key=parameter.key,
                    required_value="bitwarden_collection_id or bitwarden_item_id",
                )
            if parameter.bitwarden_collection_id and not parameter.url_parameter_key:
                raise WorkflowParameterMissingRequiredValue(
                    workflow_parameter_type=ParameterType.BITWARDEN_LOGIN_CREDENTIAL,
                    workflow_parameter_key=parameter.key,
                    required_value="url_parameter_key",
                )
            parameters[parameter.key] = BitwardenLoginCredentialParameter(
                bitwarden_login_credential_parameter_id=generate_bitwarden_login_credential_parameter_id(),
                workflow_id=workflow_id,
                bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                url_parameter_key=parameter.url_parameter_key,
                key=parameter.key,
                description=parameter.description,
                bitwarden_collection_id=parameter.bitwarden_collection_id,
                bitwarden_item_id=parameter.bitwarden_item_id,
                created_at=now,
                modified_at=now,
            )
        elif parameter.parameter_type == ParameterType.BITWARDEN_SENSITIVE_INFORMATION:
            parameters[parameter.key] = BitwardenSensitiveInformationParameter(
                bitwarden_sensitive_information_parameter_id=generate_bitwarden_sensitive_information_parameter_id(),
                workflow_id=workflow_id,
                bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                # TODO: remove "# type: ignore" after ensuring bitwarden_collection_id is always set
                bitwarden_collection_id=parameter.bitwarden_collection_id,  # type: ignore
                bitwarden_identity_key=parameter.bitwarden_identity_key,
                bitwarden_identity_fields=parameter.bitwarden_identity_fields,
                key=parameter.key,
                description=parameter.description,
                created_at=now,
                modified_at=now,
            )
        elif parameter.parameter_type == ParameterType.BITWARDEN_CREDIT_CARD_DATA:
            parameters[parameter.key] = BitwardenCreditCardDataParameter(
                bitwarden_credit_card_data_parameter_id=generate_bitwarden_credit_card_data_parameter_id(),
                workflow_id=workflow_id,
                bitwarden_client_id_aws_secret_key=parameter.bitwarden_client_id_aws_secret_key,
                bitwarden_client_secret_aws_secret_key=parameter.bitwarden_client_secret_aws_secret_key,
                bitwarden_master_password_aws_secret_key=parameter.bitwarden_master_password_aws_secret_key,
                # TODO: remove "# type: ignore" after ensuring bitwarden_collection_id is always set
                bitwarden_collection_id=parameter.bitwarden_collection_id,  # type: ignore
                bitwarden_item_id=parameter.bitwarden_item_id,  # type: ignore
                key=parameter.key,
                description=parameter.description,
                created_at=now,
                modified_at=now,
            )
        elif parameter.parameter_type == ParameterType.WORKFLOW:
            default_value = parameter.workflow_parameter_type.convert_value(
                json.dumps(parameter.default_value)
                if parameter.workflow_parameter_type == WorkflowParameterType.JSON
                else parameter.default_value
            )
            parameters[parameter.key] = WorkflowParameter(
                workflow_parameter_id=generate_workflow_parameter_id(),
                workflow_parameter_type=parameter.workflow_parameter_type,
                workflow_id=workflow_id,
                key=parameter.key,
                default_value=default_value,
                description=parameter.description,
                created_at=now,
                modified_at=now,
            )
        elif parameter.parameter_type == ParameterType.OUTPUT:
            parameters[parameter.key] = OutputParameter(
                output_parameter_id=generate_output_parameter_id(),
                workflow_id=workflow_id,
                key=parameter.key,
                description=parameter.description,
                created_at=now,
                modified_at=now,
            )
        elif parameter.parameter_type == ParameterType.CONTEXT:
            context_parameter_yamls.append(parameter)
        else:
            LOG.error(f"Invalid parameter type {parameter.parameter_type}")

    # Now we can process the context parameters since all other parameters have been created
    for context_parameter in context_parameter_yamls:
        if context_parameter.source_parameter_key not in parameters:
            raise ContextParameterSourceNotDefined(
                context_parameter_key=context_parameter.key,
                source_key=context_parameter.source_parameter_key,
            )

        if context_parameter.key in parameters:
            LOG.error(f"Duplicate parameter key {context_parameter.key}")
            duplicate_parameter_keys.add(context_parameter.key)
            continue

        # We're only adding the context parameter to the parameters dict, we're not creating it in the database
        # It'll only be stored in the `workflow.workflow_definition`
        # todo (kerem): should we have a database table for context parameters?
        parameters[context_parameter.key] = ContextParameter(
            key=context_parameter.key,
            description=context_parameter.description,
            source=parameters[context_parameter.source_parameter_key],
            # Context parameters don't have a default value, the value always depends on the source parameter
            value=None,
        )

    if duplicate_parameter_keys:
        raise WorkflowDefinitionHasDuplicateParameterKeys(duplicate_keys=duplicate_parameter_keys)

    # Validate that all blocks reference defined parameters
    undefined_parameters = _collect_undefined_parameters(workflow_definition_yaml.blocks, parameters)
    if undefined_parameters:
        raise WorkflowDefinitionHasUndefinedParameters(undefined_parameters=undefined_parameters)

    # Create blocks from the request
    block_label_mapping = {}
    blocks: list[BlockTypeVar] = []
    for block_yaml in workflow_definition_yaml.blocks:
        block = block_yaml_to_block(block_yaml, parameters)
        blocks.append(block)
        block_label_mapping[block.label] = block

    # Set the blocks for the workflow definition and derive DAG version metadata
    dag_version = workflow_definition_yaml.version
    if dag_version is None:
        dag_version = 2 if _has_dag_metadata(workflow_definition_yaml.blocks) else 1

    workflow_definition = WorkflowDefinition(
        parameters=parameters.values(),
        blocks=blocks,
        version=dag_version,
        finally_block_label=workflow_definition_yaml.finally_block_label,
    )

    LOG.info(
        "Created workflow from request",
        parameter_keys=[parameter.key for parameter in parameters.values()],
        block_labels=[block.label for block in blocks],
        workflow_id=workflow_id,
    )

    return workflow_definition


def _create_all_output_parameters_for_workflow(
    workflow_id: str, block_yamls: list[BLOCK_YAML_TYPES]
) -> dict[str, OutputParameter]:
    output_parameters = {}
    for block_yaml in block_yamls:
        output_parameter_key = f"{block_yaml.label}_output"
        output_parameter = OutputParameter(
            output_parameter_id=generate_output_parameter_id(),
            key=output_parameter_key,
            description=f"Output parameter for block {block_yaml.label}",
            workflow_id=workflow_id,
            created_at=datetime.utcnow(),
            modified_at=datetime.utcnow(),
        )
        output_parameters[block_yaml.label] = output_parameter
        # Recursively create output parameters for for-loop blocks
        if isinstance(block_yaml, ForLoopBlockYAML):
            output_parameters.update(
                _create_all_output_parameters_for_workflow(workflow_id=workflow_id, block_yamls=block_yaml.loop_blocks)
            )
    return output_parameters


def _build_block_kwargs(
    block_yaml: BLOCK_YAML_TYPES,
    output_parameter: OutputParameter,
) -> dict[str, Any]:
    return {
        "label": block_yaml.label,
        "next_block_label": block_yaml.next_block_label,
        "output_parameter": output_parameter,
        "continue_on_failure": block_yaml.continue_on_failure,
        "next_loop_on_failure": block_yaml.next_loop_on_failure,
        "model": block_yaml.model,
    }


def block_yaml_to_block(
    block_yaml: BLOCK_YAML_TYPES,
    parameters: dict[str, PARAMETER_TYPE],
) -> BlockTypeVar:
    output_parameter = cast(OutputParameter, parameters[f"{block_yaml.label}_output"])
    base_kwargs = _build_block_kwargs(block_yaml, output_parameter)
    if block_yaml.block_type == BlockType.TASK:
        task_block_parameters = _resolve_block_parameters(block_yaml, parameters)
        return TaskBlock(
            **base_kwargs,
            url=block_yaml.url,
            title=block_yaml.title,
            engine=block_yaml.engine,
            parameters=task_block_parameters,
            navigation_goal=block_yaml.navigation_goal,
            data_extraction_goal=block_yaml.data_extraction_goal,
            data_schema=block_yaml.data_schema,
            error_code_mapping=block_yaml.error_code_mapping,
            max_steps_per_run=block_yaml.max_steps_per_run,
            max_retries=block_yaml.max_retries,
            complete_on_download=block_yaml.complete_on_download,
            download_suffix=block_yaml.download_suffix,
            totp_verification_url=block_yaml.totp_verification_url,
            totp_identifier=block_yaml.totp_identifier,
            disable_cache=block_yaml.disable_cache,
            complete_criterion=block_yaml.complete_criterion,
            terminate_criterion=block_yaml.terminate_criterion,
            complete_verification=block_yaml.complete_verification,
            include_action_history_in_verification=block_yaml.include_action_history_in_verification,
        )
    elif block_yaml.block_type == BlockType.FOR_LOOP:
        loop_blocks = [block_yaml_to_block(loop_block, parameters) for loop_block in block_yaml.loop_blocks]

        loop_over_parameter: Parameter | None = None
        if block_yaml.loop_over_parameter_key:
            loop_over_parameter = parameters[block_yaml.loop_over_parameter_key]

        if block_yaml.loop_variable_reference:
            # it's backaward compatible with jinja style parameter and context paramter
            # we trim the format like {{ loop_key }} into loop_key to initialize the context parater,
            # otherwise it might break the context parameter initialization chain, blow up the worklofw parameters
            # TODO: consider remove this if we totally give up context parameter
            trimmed_key = block_yaml.loop_variable_reference.strip(" {}")
            if trimmed_key in parameters:
                loop_over_parameter = parameters[trimmed_key]

        if loop_over_parameter is None and not block_yaml.loop_variable_reference:
            raise InvalidWorkflowDefinition(
                f"For loop block '{block_yaml.label}' requires either loop_over_parameter_key or loop_variable_reference"
            )

        return ForLoopBlock(
            **base_kwargs,
            loop_over=loop_over_parameter,
            loop_variable_reference=block_yaml.loop_variable_reference,
            loop_blocks=loop_blocks,
            complete_if_empty=block_yaml.complete_if_empty,
        )
    elif block_yaml.block_type == BlockType.CONDITIONAL:
        branch_conditions = []
        for branch in block_yaml.branch_conditions:
            branch_criteria = None
            if branch.criteria:
                if branch.criteria.criteria_type == "prompt":
                    branch_criteria = PromptBranchCriteria(
                        criteria_type=branch.criteria.criteria_type,
                        expression=branch.criteria.expression,
                        description=branch.criteria.description,
                    )
                else:
                    branch_criteria = JinjaBranchCriteria(
                        criteria_type=branch.criteria.criteria_type,
                        expression=branch.criteria.expression,
                        description=branch.criteria.description,
                    )

            branch_conditions.append(
                BranchCondition(
                    criteria=branch_criteria,
                    next_block_label=branch.next_block_label,
                    description=branch.description,
                    is_default=branch.is_default,
                )
            )

        return ConditionalBlock(
            **base_kwargs,
            branch_conditions=branch_conditions,
        )
    elif block_yaml.block_type == BlockType.CODE:
        return CodeBlock(
            **base_kwargs,
            code=block_yaml.code,
            parameters=_resolve_block_parameters(block_yaml, parameters),
        )
    elif block_yaml.block_type == BlockType.TEXT_PROMPT:
        return TextPromptBlock(
            **base_kwargs,
            llm_key=block_yaml.llm_key,
            prompt=block_yaml.prompt,
            parameters=_resolve_block_parameters(block_yaml, parameters),
            json_schema=block_yaml.json_schema,
        )
    elif block_yaml.block_type == BlockType.DOWNLOAD_TO_S3:
        return DownloadToS3Block(
            **base_kwargs,
            url=block_yaml.url,
        )
    elif block_yaml.block_type == BlockType.UPLOAD_TO_S3:
        return UploadToS3Block(
            **base_kwargs,
            path=block_yaml.path,
        )
    elif block_yaml.block_type == BlockType.FILE_UPLOAD:
        return FileUploadBlock(
            **base_kwargs,
            storage_type=block_yaml.storage_type,
            s3_bucket=block_yaml.s3_bucket,
            aws_access_key_id=block_yaml.aws_access_key_id,
            aws_secret_access_key=block_yaml.aws_secret_access_key,
            region_name=block_yaml.region_name,
            azure_storage_account_name=block_yaml.azure_storage_account_name,
            azure_storage_account_key=block_yaml.azure_storage_account_key,
            azure_blob_container_name=block_yaml.azure_blob_container_name,
            path=block_yaml.path,
        )
    elif block_yaml.block_type == BlockType.SEND_EMAIL:
        return SendEmailBlock(
            **base_kwargs,
            smtp_host=parameters[block_yaml.smtp_host_secret_parameter_key],
            smtp_port=parameters[block_yaml.smtp_port_secret_parameter_key],
            smtp_username=parameters[block_yaml.smtp_username_secret_parameter_key],
            smtp_password=parameters[block_yaml.smtp_password_secret_parameter_key],
            sender=block_yaml.sender,
            recipients=block_yaml.recipients,
            subject=block_yaml.subject,
            body=block_yaml.body,
            file_attachments=block_yaml.file_attachments or [],
        )
    elif block_yaml.block_type == BlockType.FILE_URL_PARSER:
        return FileParserBlock(
            **base_kwargs,
            file_url=block_yaml.file_url,
            file_type=block_yaml.file_type,
            json_schema=block_yaml.json_schema,
        )
    elif block_yaml.block_type == BlockType.PDF_PARSER:
        return PDFParserBlock(
            **base_kwargs,
            file_url=block_yaml.file_url,
            json_schema=block_yaml.json_schema,
        )
    elif block_yaml.block_type == BlockType.VALIDATION:
        validation_block_parameters = _resolve_block_parameters(block_yaml, parameters)

        if not block_yaml.complete_criterion and not block_yaml.terminate_criterion:
            raise InvalidWorkflowDefinition(
                f"Validation block '{block_yaml.label}' requires at least one of complete_criterion or terminate_criterion"
            )

        return ValidationBlock(
            **base_kwargs,
            task_type=TaskType.validation,
            parameters=validation_block_parameters,
            complete_criterion=block_yaml.complete_criterion,
            terminate_criterion=block_yaml.terminate_criterion,
            error_code_mapping=block_yaml.error_code_mapping,
            # Should only need one step for validation block, but we allow 2 in case the LLM has an unexpected failure and we need to retry.
            max_steps_per_run=2,
        )

    elif block_yaml.block_type == BlockType.ACTION:
        action_block_parameters = _resolve_block_parameters(block_yaml, parameters)

        if not block_yaml.navigation_goal:
            raise InvalidWorkflowDefinition(f"Action block '{block_yaml.label}' requires navigation_goal")

        return ActionBlock(
            **base_kwargs,
            url=block_yaml.url,
            title=block_yaml.title,
            engine=block_yaml.engine,
            task_type=TaskType.action,
            parameters=action_block_parameters,
            navigation_goal=block_yaml.navigation_goal,
            error_code_mapping=block_yaml.error_code_mapping,
            max_retries=block_yaml.max_retries,
            complete_on_download=block_yaml.complete_on_download,
            download_suffix=block_yaml.download_suffix,
            totp_verification_url=block_yaml.totp_verification_url,
            totp_identifier=block_yaml.totp_identifier,
            disable_cache=block_yaml.disable_cache,
            # DO NOT run complete verification for action block
            complete_verification=False,
            max_steps_per_run=1,
        )

    elif block_yaml.block_type == BlockType.NAVIGATION:
        navigation_block_parameters = _resolve_block_parameters(block_yaml, parameters)
        return NavigationBlock(
            **base_kwargs,
            url=block_yaml.url,
            title=block_yaml.title,
            engine=block_yaml.engine,
            parameters=navigation_block_parameters,
            navigation_goal=block_yaml.navigation_goal,
            error_code_mapping=block_yaml.error_code_mapping,
            max_steps_per_run=block_yaml.max_steps_per_run,
            max_retries=block_yaml.max_retries,
            complete_on_download=block_yaml.complete_on_download,
            download_suffix=block_yaml.download_suffix,
            totp_verification_url=block_yaml.totp_verification_url,
            totp_identifier=block_yaml.totp_identifier,
            disable_cache=block_yaml.disable_cache,
            complete_criterion=block_yaml.complete_criterion,
            terminate_criterion=block_yaml.terminate_criterion,
            complete_verification=block_yaml.complete_verification,
            include_action_history_in_verification=block_yaml.include_action_history_in_verification,
        )

    elif block_yaml.block_type == BlockType.HUMAN_INTERACTION:
        return HumanInteractionBlock(
            **base_kwargs,
            instructions=block_yaml.instructions,
            positive_descriptor=block_yaml.positive_descriptor,
            negative_descriptor=block_yaml.negative_descriptor,
            timeout_seconds=block_yaml.timeout_seconds,
            # --
            sender=block_yaml.sender,
            recipients=block_yaml.recipients,
            subject=block_yaml.subject,
            body=block_yaml.body,
        )

    elif block_yaml.block_type == BlockType.EXTRACTION:
        extraction_block_parameters = _resolve_block_parameters(block_yaml, parameters)
        return ExtractionBlock(
            **base_kwargs,
            url=block_yaml.url,
            title=block_yaml.title,
            engine=block_yaml.engine,
            parameters=extraction_block_parameters,
            data_extraction_goal=block_yaml.data_extraction_goal,
            data_schema=block_yaml.data_schema,
            max_steps_per_run=block_yaml.max_steps_per_run,
            max_retries=block_yaml.max_retries,
            disable_cache=block_yaml.disable_cache,
            complete_verification=False,
        )

    elif block_yaml.block_type == BlockType.LOGIN:
        login_block_parameters = _resolve_block_parameters(block_yaml, parameters)
        return LoginBlock(
            **base_kwargs,
            url=block_yaml.url,
            title=block_yaml.title,
            engine=block_yaml.engine,
            parameters=login_block_parameters,
            navigation_goal=block_yaml.navigation_goal,
            error_code_mapping=block_yaml.error_code_mapping,
            max_steps_per_run=block_yaml.max_steps_per_run,
            max_retries=block_yaml.max_retries,
            totp_verification_url=block_yaml.totp_verification_url,
            totp_identifier=block_yaml.totp_identifier,
            disable_cache=block_yaml.disable_cache,
            complete_criterion=block_yaml.complete_criterion,
            terminate_criterion=block_yaml.terminate_criterion,
            complete_verification=block_yaml.complete_verification,
        )

    elif block_yaml.block_type == BlockType.WAIT:
        if block_yaml.wait_sec <= 0 or block_yaml.wait_sec > settings.WORKFLOW_WAIT_BLOCK_MAX_SEC:
            raise InvalidWaitBlockTime(settings.WORKFLOW_WAIT_BLOCK_MAX_SEC)

        return WaitBlock(
            **base_kwargs,
            wait_sec=block_yaml.wait_sec,
        )

    elif block_yaml.block_type == BlockType.FILE_DOWNLOAD:
        file_download_block_parameters = _resolve_block_parameters(block_yaml, parameters)
        return FileDownloadBlock(
            **base_kwargs,
            url=block_yaml.url,
            title=block_yaml.title,
            engine=block_yaml.engine,
            parameters=file_download_block_parameters,
            navigation_goal=block_yaml.navigation_goal,
            error_code_mapping=block_yaml.error_code_mapping,
            max_steps_per_run=block_yaml.max_steps_per_run,
            max_retries=block_yaml.max_retries,
            download_suffix=block_yaml.download_suffix,
            totp_verification_url=block_yaml.totp_verification_url,
            totp_identifier=block_yaml.totp_identifier,
            disable_cache=block_yaml.disable_cache,
            complete_on_download=True,
            complete_verification=True,
            include_action_history_in_verification=True,
            download_timeout=block_yaml.download_timeout,
        )
    elif block_yaml.block_type == BlockType.TaskV2:
        return TaskV2Block(
            **base_kwargs,
            prompt=block_yaml.prompt,
            url=block_yaml.url,
            totp_verification_url=block_yaml.totp_verification_url,
            totp_identifier=block_yaml.totp_identifier,
            max_iterations=block_yaml.max_iterations,
            max_steps=block_yaml.max_steps,
        )
    elif block_yaml.block_type == BlockType.HTTP_REQUEST:
        http_request_block_parameters = _resolve_block_parameters(block_yaml, parameters)
        return HttpRequestBlock(
            **base_kwargs,
            method=block_yaml.method,
            url=block_yaml.url,
            headers=block_yaml.headers,
            body=block_yaml.body,
            files=block_yaml.files,
            timeout=block_yaml.timeout,
            follow_redirects=block_yaml.follow_redirects,
            download_filename=block_yaml.download_filename,
            save_response_as_file=block_yaml.save_response_as_file,
            parameters=http_request_block_parameters,
        )
    elif block_yaml.block_type == BlockType.GOTO_URL:
        return UrlBlock(
            **base_kwargs,
            url=block_yaml.url,
            complete_verification=False,
        )
    elif block_yaml.block_type == BlockType.PRINT_PAGE:
        return PrintPageBlock(
            **base_kwargs,
            include_timestamp=block_yaml.include_timestamp,
            custom_filename=block_yaml.custom_filename,
            format=block_yaml.format,
            landscape=block_yaml.landscape,
            print_background=block_yaml.print_background,
        )

    raise ValueError(f"Invalid block type {block_yaml.block_type}")


def _collect_undefined_parameters(
    block_yamls: list[BLOCK_YAML_TYPES],
    parameters: dict[str, PARAMETER_TYPE],
) -> dict[str, list[str]]:
    """
    Collect all undefined parameters referenced by blocks (including nested blocks in for_loop).
    Returns a dict mapping block labels to lists of undefined parameter keys.
    """
    undefined_params: dict[str, list[str]] = {}

    for block_yaml in block_yamls:
        # Check parameters for this block
        if block_yaml.parameter_keys:
            undefined_for_block = [param_key for param_key in block_yaml.parameter_keys if param_key not in parameters]
            if undefined_for_block:
                undefined_params[block_yaml.label] = undefined_for_block

        # Recursively check nested blocks in for_loop
        if isinstance(block_yaml, ForLoopBlockYAML) and block_yaml.loop_blocks:
            nested_undefined = _collect_undefined_parameters(block_yaml.loop_blocks, parameters)
            undefined_params.update(nested_undefined)

    return undefined_params


def _resolve_block_parameters(
    block_yaml: BLOCK_YAML_TYPES,
    parameters: dict[str, PARAMETER_TYPE],
) -> list[PARAMETER_TYPE]:
    return (
        [parameters[parameter_key] for parameter_key in block_yaml.parameter_keys] if block_yaml.parameter_keys else []
    )


def _has_dag_metadata(block_yamls: list[BLOCK_YAML_TYPES]) -> bool:
    for block_yaml in block_yamls:
        if block_yaml.next_block_label:
            return True
        if isinstance(block_yaml, ForLoopBlockYAML) and _has_dag_metadata(block_yaml.loop_blocks):
            return True
    return False
