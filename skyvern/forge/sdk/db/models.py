import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UnicodeText,
    UniqueConstraint,
    desc,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase

from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType, TaskType
from skyvern.forge.sdk.db.id import (
    generate_action_id,
    generate_ai_suggestion_id,
    generate_artifact_id,
    generate_aws_secret_parameter_id,
    generate_bitwarden_credit_card_data_parameter_id,
    generate_bitwarden_login_credential_parameter_id,
    generate_bitwarden_sensitive_information_parameter_id,
    generate_credential_id,
    generate_credential_parameter_id,
    generate_org_id,
    generate_organization_auth_token_id,
    generate_organization_bitwarden_collection_id,
    generate_output_parameter_id,
    generate_persistent_browser_session_id,
    generate_step_id,
    generate_task_generation_id,
    generate_task_id,
    generate_task_run_id,
    generate_task_v2_id,
    generate_thought_id,
    generate_totp_code_id,
    generate_workflow_id,
    generate_workflow_parameter_id,
    generate_workflow_permanent_id,
    generate_workflow_run_block_id,
    generate_workflow_run_id,
)
from skyvern.forge.sdk.schemas.task_v2 import ThoughtType


class Base(AsyncAttrs, DeclarativeBase):
    pass


class TaskModel(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("idx_tasks_org_created", "organization_id", "created_at"),)

    task_id = Column(String, primary_key=True, index=True, default=generate_task_id)
    organization_id = Column(String, ForeignKey("organizations.organization_id"))
    status = Column(String, index=True)
    webhook_callback_url = Column(String)
    totp_verification_url = Column(String)
    totp_identifier = Column(String)
    title = Column(String)
    task_type = Column(String, default=TaskType.general)
    url = Column(String)
    navigation_goal = Column(String)
    data_extraction_goal = Column(String)
    complete_criterion = Column(String)
    terminate_criterion = Column(String)
    navigation_payload = Column(JSON)
    extracted_information = Column(JSON)
    failure_reason = Column(String)
    proxy_location = Column(String)
    extracted_information_schema = Column(JSON)
    workflow_run_id = Column(String, ForeignKey("workflow_runs.workflow_run_id"), index=True)
    order = Column(Integer, nullable=True)
    retry = Column(Integer, nullable=True)
    error_code_mapping = Column(JSON, nullable=True)
    errors = Column(JSON, default=[], nullable=False)
    max_steps_per_run = Column(Integer, nullable=True)
    application = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False, index=True)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
        index=True,
    )


class StepModel(Base):
    __tablename__ = "steps"
    __table_args__ = (
        Index("org_task_index", "organization_id", "task_id"),
        Index("created_at_org_index", "created_at", "organization_id"),
    )

    step_id = Column(String, primary_key=True, index=True, default=generate_step_id)
    organization_id = Column(String, ForeignKey("organizations.organization_id"))
    task_id = Column(String, ForeignKey("tasks.task_id"), index=True)
    status = Column(String)
    output = Column(JSON)
    order = Column(Integer)
    is_last = Column(Boolean, default=False)
    retry_index = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )
    input_token_count = Column(Integer, default=0)
    output_token_count = Column(Integer, default=0)
    step_cost = Column(Numeric, default=0)


class OrganizationModel(Base):
    __tablename__ = "organizations"

    organization_id = Column(String, primary_key=True, index=True, default=generate_org_id)
    organization_name = Column(String, nullable=False)
    webhook_callback_url = Column(UnicodeText)
    max_steps_per_run = Column(Integer, nullable=True)
    max_retries_per_step = Column(Integer, nullable=True)
    domain = Column(String, nullable=True, index=True)
    bw_organization_id = Column(String, nullable=True, default=None)
    bw_collection_ids = Column(JSON, nullable=True, default=None)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )


class OrganizationAuthTokenModel(Base):
    __tablename__ = "organization_auth_tokens"

    id = Column(
        String,
        primary_key=True,
        index=True,
        default=generate_organization_auth_token_id,
    )

    organization_id = Column(String, ForeignKey("organizations.organization_id"), index=True, nullable=False)
    token_type = Column(Enum(OrganizationAuthTokenType), nullable=False)
    token = Column(String, index=True, nullable=False)
    valid = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )
    deleted_at = Column(DateTime, nullable=True)


class ArtifactModel(Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("org_task_step_index", "organization_id", "task_id", "step_id"),)

    artifact_id = Column(String, primary_key=True, index=True, default=generate_artifact_id)
    organization_id = Column(String, ForeignKey("organizations.organization_id"))
    workflow_run_id = Column(String, index=True)
    workflow_run_block_id = Column(String, index=True)
    observer_cruise_id = Column(String, index=True)
    observer_thought_id = Column(String, index=True)
    ai_suggestion_id = Column(String, index=True)
    task_id = Column(String, ForeignKey("tasks.task_id"))
    step_id = Column(String, ForeignKey("steps.step_id"), index=True)
    artifact_type = Column(String)
    uri = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )


class WorkflowModel(Base):
    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "workflow_permanent_id",
            "version",
            name="uc_org_permanent_id_version",
        ),
        Index("permanent_id_version_idx", "workflow_permanent_id", "version"),
        Index("organization_id_title_idx", "organization_id", "title"),
        Index("workflow_oid_status_idx", "organization_id", "status"),
    )

    workflow_id = Column(String, primary_key=True, index=True, default=generate_workflow_id)
    organization_id = Column(String, ForeignKey("organizations.organization_id"))
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    workflow_definition = Column(JSON, nullable=False)
    proxy_location = Column(String)
    webhook_callback_url = Column(String)
    totp_verification_url = Column(String)
    totp_identifier = Column(String)
    persist_browser_session = Column(Boolean, default=False, nullable=False)
    status = Column(String, nullable=False, default="published")

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )
    deleted_at = Column(DateTime, nullable=True)

    workflow_permanent_id = Column(String, nullable=False, default=generate_workflow_permanent_id, index=True)
    version = Column(Integer, default=1, nullable=False)
    is_saved_task = Column(Boolean, default=False, nullable=False)


class WorkflowRunModel(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (Index("idx_workflow_runs_org_created", "organization_id", "created_at"),)

    workflow_run_id = Column(String, primary_key=True, index=True, default=generate_workflow_run_id)
    workflow_id = Column(String, ForeignKey("workflows.workflow_id"), nullable=False)
    workflow_permanent_id = Column(String, nullable=False, index=True)
    # workfow runs with parent_workflow_run_id are nested workflow runs which won't show up in the workflow run history
    parent_workflow_run_id = Column(String, ForeignKey("workflow_runs.workflow_run_id"), nullable=True, index=True)
    organization_id = Column(String, ForeignKey("organizations.organization_id"), nullable=False, index=True)
    status = Column(String, nullable=False)
    failure_reason = Column(String)
    proxy_location = Column(String)
    webhook_callback_url = Column(String)
    totp_verification_url = Column(String)
    totp_identifier = Column(String)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
        index=True,
    )


class WorkflowParameterModel(Base):
    __tablename__ = "workflow_parameters"

    workflow_parameter_id = Column(String, primary_key=True, index=True, default=generate_workflow_parameter_id)
    workflow_parameter_type = Column(String, nullable=False)
    key = Column(String, nullable=False)
    description = Column(String, nullable=True)
    workflow_id = Column(String, ForeignKey("workflows.workflow_id"), index=True, nullable=False)
    default_value = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )
    deleted_at = Column(DateTime, nullable=True)


class OutputParameterModel(Base):
    __tablename__ = "output_parameters"

    output_parameter_id = Column(String, primary_key=True, index=True, default=generate_output_parameter_id)
    key = Column(String, nullable=False)
    description = Column(String, nullable=True)
    workflow_id = Column(String, ForeignKey("workflows.workflow_id"), index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )
    deleted_at = Column(DateTime, nullable=True)


class AWSSecretParameterModel(Base):
    __tablename__ = "aws_secret_parameters"

    aws_secret_parameter_id = Column(String, primary_key=True, index=True, default=generate_aws_secret_parameter_id)
    workflow_id = Column(String, ForeignKey("workflows.workflow_id"), index=True, nullable=False)
    key = Column(String, nullable=False)
    description = Column(String, nullable=True)
    aws_key = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )
    deleted_at = Column(DateTime, nullable=True)


class BitwardenLoginCredentialParameterModel(Base):
    __tablename__ = "bitwarden_login_credential_parameters"

    bitwarden_login_credential_parameter_id = Column(
        String,
        primary_key=True,
        index=True,
        default=generate_bitwarden_login_credential_parameter_id,
    )
    workflow_id = Column(String, ForeignKey("workflows.workflow_id"), index=True, nullable=False)
    key = Column(String, nullable=False)
    description = Column(String, nullable=True)
    bitwarden_client_id_aws_secret_key = Column(String, nullable=False)
    bitwarden_client_secret_aws_secret_key = Column(String, nullable=False)
    bitwarden_master_password_aws_secret_key = Column(String, nullable=False)
    bitwarden_collection_id = Column(String, nullable=True, default=None)
    bitwarden_item_id = Column(String, nullable=True, default=None)
    url_parameter_key = Column(String, nullable=True, default=None)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )
    deleted_at = Column(DateTime, nullable=True)


class BitwardenSensitiveInformationParameterModel(Base):
    __tablename__ = "bitwarden_sensitive_information_parameters"

    bitwarden_sensitive_information_parameter_id = Column(
        String,
        primary_key=True,
        index=True,
        default=generate_bitwarden_sensitive_information_parameter_id,
    )
    workflow_id = Column(String, ForeignKey("workflows.workflow_id"), index=True, nullable=False)
    key = Column(String, nullable=False)
    description = Column(String, nullable=True)
    bitwarden_client_id_aws_secret_key = Column(String, nullable=False)
    bitwarden_client_secret_aws_secret_key = Column(String, nullable=False)
    bitwarden_master_password_aws_secret_key = Column(String, nullable=False)
    bitwarden_collection_id = Column(String, nullable=False)
    bitwarden_identity_key = Column(String, nullable=False)
    # This is a list of fields to extract from the Bitwarden Identity.
    bitwarden_identity_fields = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )
    deleted_at = Column(DateTime, nullable=True)


class BitwardenCreditCardDataParameterModel(Base):
    __tablename__ = "bitwarden_credit_card_data_parameters"

    bitwarden_credit_card_data_parameter_id = Column(
        String,
        primary_key=True,
        index=True,
        default=generate_bitwarden_credit_card_data_parameter_id,
    )
    workflow_id = Column(String, ForeignKey("workflows.workflow_id"), index=True, nullable=False)
    key = Column(String, nullable=False)
    description = Column(String, nullable=True)
    bitwarden_client_id_aws_secret_key = Column(String, nullable=False)
    bitwarden_client_secret_aws_secret_key = Column(String, nullable=False)
    bitwarden_master_password_aws_secret_key = Column(String, nullable=False)
    bitwarden_collection_id = Column(String, nullable=False)
    bitwarden_item_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)
    deleted_at = Column(DateTime, nullable=True)


class CredentialParameterModel(Base):
    __tablename__ = "credential_parameters"

    credential_parameter_id = Column(String, primary_key=True, index=True, default=generate_credential_parameter_id)
    workflow_id = Column(String, ForeignKey("workflows.workflow_id"), index=True, nullable=False)
    key = Column(String, nullable=False)
    description = Column(String, nullable=True)

    credential_id = Column(String, nullable=False)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)
    deleted_at = Column(DateTime, nullable=True)


class WorkflowRunParameterModel(Base):
    __tablename__ = "workflow_run_parameters"

    workflow_run_id = Column(
        String,
        ForeignKey("workflow_runs.workflow_run_id"),
        primary_key=True,
        index=True,
    )
    workflow_parameter_id = Column(
        String,
        ForeignKey("workflow_parameters.workflow_parameter_id"),
        primary_key=True,
        index=True,
    )
    # Can be bool | int | float | str | dict | list depending on the workflow parameter type
    value = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)


class WorkflowRunOutputParameterModel(Base):
    __tablename__ = "workflow_run_output_parameters"

    workflow_run_id = Column(
        String,
        ForeignKey("workflow_runs.workflow_run_id"),
        primary_key=True,
        index=True,
    )
    output_parameter_id = Column(
        String,
        ForeignKey("output_parameters.output_parameter_id"),
        primary_key=True,
        index=True,
    )
    value = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)


class TaskGenerationModel(Base):
    """
    Generate a task based on the prompt (natural language description of the task) from the user
    """

    __tablename__ = "task_generations"

    task_generation_id = Column(String, primary_key=True, default=generate_task_generation_id)
    organization_id = Column(String, ForeignKey("organizations.organization_id"), nullable=False)
    user_prompt = Column(String, nullable=False)
    user_prompt_hash = Column(String, index=True)
    url = Column(String)
    navigation_goal = Column(String)
    navigation_payload = Column(JSON)
    data_extraction_goal = Column(String)
    extracted_information_schema = Column(JSON)
    suggested_title = Column(String)  # task title suggested by the language model

    llm = Column(String)  # language model to use
    llm_prompt = Column(String)  # The prompt sent to the language model
    llm_response = Column(String)  # The response from the language model

    source_task_generation_id = Column(String, index=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)


class AISuggestionModel(Base):
    __tablename__ = "ai_suggestions"

    ai_suggestion_id = Column(String, primary_key=True, default=generate_ai_suggestion_id)
    organization_id = Column(String, ForeignKey("organizations.organization_id"))
    ai_suggestion_type = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)


class TOTPCodeModel(Base):
    __tablename__ = "totp_codes"

    totp_code_id = Column(String, primary_key=True, default=generate_totp_code_id)
    totp_identifier = Column(String, nullable=False, index=True)
    organization_id = Column(String, ForeignKey("organizations.organization_id"))
    task_id = Column(String, ForeignKey("tasks.task_id"))
    workflow_id = Column(String, ForeignKey("workflows.workflow_id"))
    content = Column(String, nullable=False)
    code = Column(String, nullable=False)
    source = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False, index=True)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)
    expired_at = Column(DateTime, index=True)


class ActionModel(Base):
    __tablename__ = "actions"
    __table_args__ = (
        Index("action_org_task_step_index", "organization_id", "task_id", "step_id"),
        Index("action_org_created_at_index", "organization_id", desc("created_at")),
    )

    action_id = Column(String, primary_key=True, index=True, default=generate_action_id)
    action_type = Column(String, nullable=False)
    source_action_id = Column(String, ForeignKey("actions.action_id"), nullable=True, index=True)
    organization_id = Column(String, ForeignKey("organizations.organization_id"), nullable=True)
    workflow_run_id = Column(String, ForeignKey("workflow_runs.workflow_run_id"), nullable=True)
    task_id = Column(String, ForeignKey("tasks.task_id"), nullable=False, index=True)
    step_id = Column(String, ForeignKey("steps.step_id"), nullable=False)
    step_order = Column(Integer, nullable=False)
    action_order = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    reasoning = Column(String, nullable=True)
    intention = Column(String, nullable=True)
    response = Column(String, nullable=True)
    element_id = Column(String, nullable=True)
    skyvern_element_hash = Column(String, nullable=True)
    skyvern_element_data = Column(JSON, nullable=True)
    action_json = Column(JSON, nullable=True)
    confidence_float = Column(Numeric, nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)


class WorkflowRunBlockModel(Base):
    __tablename__ = "workflow_run_blocks"
    __table_args__ = (Index("wfrb_org_wfr_index", "organization_id", "workflow_run_id"),)

    workflow_run_block_id = Column(String, primary_key=True, default=generate_workflow_run_block_id)
    workflow_run_id = Column(String, ForeignKey("workflow_runs.workflow_run_id"), nullable=False)
    # this is the inner workflow run id of the taskv2 block
    block_workflow_run_id = Column(String, ForeignKey("workflow_runs.workflow_run_id"), nullable=True)
    parent_workflow_run_block_id = Column(
        String, ForeignKey("workflow_run_blocks.workflow_run_block_id"), nullable=True
    )
    organization_id = Column(String, ForeignKey("organizations.organization_id"), nullable=True)
    description = Column(String, nullable=True)
    task_id = Column(String, ForeignKey("tasks.task_id"), nullable=True)
    label = Column(String, nullable=True)
    block_type = Column(String, nullable=False)
    status = Column(String, nullable=False)
    output = Column(JSON, nullable=True)
    continue_on_failure = Column(Boolean, nullable=False, default=False)
    failure_reason = Column(String, nullable=True)

    # for loop block
    loop_values = Column(JSON, nullable=True)
    current_value = Column(String, nullable=True)
    current_index = Column(Integer, nullable=True)

    # email block
    recipients = Column(JSON, nullable=True)
    attachments = Column(JSON, nullable=True)
    subject = Column(String, nullable=True)
    body = Column(String, nullable=True)

    # prompt block
    prompt = Column(String, nullable=True)

    # wait block
    wait_sec = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)


class TaskV2Model(Base):
    __tablename__ = "observer_cruises"
    __table_args__ = (Index("oc_org_wfr_index", "organization_id", "workflow_run_id"),)

    # observer_cruise_id is the task_id for task v2
    observer_cruise_id = Column(String, primary_key=True, default=generate_task_v2_id)
    status = Column(String, nullable=False, default="created")
    organization_id = Column(String, ForeignKey("organizations.organization_id"), nullable=True)
    workflow_run_id = Column(String, ForeignKey("workflow_runs.workflow_run_id"), nullable=True)
    workflow_id = Column(String, ForeignKey("workflows.workflow_id"), nullable=True)
    workflow_permanent_id = Column(String, nullable=True)
    prompt = Column(UnicodeText, nullable=True)
    url = Column(String, nullable=True)
    summary = Column(String, nullable=True)
    output = Column(JSON, nullable=True)
    webhook_callback_url = Column(String, nullable=True)
    totp_verification_url = Column(String, nullable=True)
    totp_identifier = Column(String, nullable=True)
    proxy_location = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)


class ThoughtModel(Base):
    __tablename__ = "observer_thoughts"
    __table_args__ = (Index("observer_cruise_index", "organization_id", "observer_cruise_id"),)

    observer_thought_id = Column(String, primary_key=True, default=generate_thought_id)
    organization_id = Column(String, ForeignKey("organizations.organization_id"), nullable=True)
    observer_cruise_id = Column(String, ForeignKey("observer_cruises.observer_cruise_id"), nullable=False)
    workflow_run_id = Column(String, ForeignKey("workflow_runs.workflow_run_id"), nullable=True)
    workflow_run_block_id = Column(String, ForeignKey("workflow_run_blocks.workflow_run_block_id"), nullable=True)
    workflow_id = Column(String, ForeignKey("workflows.workflow_id"), nullable=True)
    workflow_permanent_id = Column(String, nullable=True)
    user_input = Column(UnicodeText, nullable=True)
    observation = Column(String, nullable=True)
    thought = Column(String, nullable=True)
    answer = Column(String, nullable=True)
    input_token_count = Column(Integer, nullable=True)
    output_token_count = Column(Integer, nullable=True)
    thought_cost = Column(Numeric, nullable=True)

    observer_thought_type = Column(String, nullable=True, default=ThoughtType.plan)
    observer_thought_scenario = Column(String, nullable=True)
    output = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)


class PersistentBrowserSessionModel(Base):
    __tablename__ = "persistent_browser_sessions"

    persistent_browser_session_id = Column(String, primary_key=True, default=generate_persistent_browser_session_id)
    organization_id = Column(String, ForeignKey("organizations.organization_id"), nullable=False)
    runnable_type = Column(String, nullable=True)
    runnable_id = Column(String, nullable=True)
    browser_id = Column(String, nullable=True)
    browser_address = Column(String, nullable=True)
    status = Column(String, nullable=True, default="created")
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)
    deleted_at = Column(DateTime, nullable=True)


class TaskRunModel(Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        Index("task_run_org_url_index", "organization_id", "url_hash", "cached"),
        Index("task_run_org_run_id_index", "organization_id", "run_id"),
    )

    task_run_id = Column(String, primary_key=True, default=generate_task_run_id)
    organization_id = Column(String, nullable=False)
    task_run_type = Column(String, nullable=False)
    run_id = Column(String, nullable=False)
    title = Column(String, nullable=True)
    url = Column(String, nullable=True)
    url_hash = Column(String, nullable=True)
    cached = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)


class OrganizationBitwardenCollectionModel(Base):
    __tablename__ = "organization_bitwarden_collections"

    organization_bitwarden_collection_id = Column(
        String, primary_key=True, default=generate_organization_bitwarden_collection_id
    )

    organization_id = Column(String, nullable=False, index=True)
    collection_id = Column(String, nullable=False)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)


class CredentialModel(Base):
    __tablename__ = "credentials"

    credential_id = Column(String, primary_key=True, default=generate_credential_id)
    organization_id = Column(String, nullable=False)
    item_id = Column(String, nullable=True)

    name = Column(String, nullable=False)
    credential_type = Column(String, nullable=False)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)
    deleted_at = Column(DateTime, nullable=True)
