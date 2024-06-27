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
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase

from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.db.id import (
    generate_artifact_id,
    generate_aws_secret_parameter_id,
    generate_bitwarden_login_credential_parameter_id,
    generate_org_id,
    generate_organization_auth_token_id,
    generate_output_parameter_id,
    generate_step_id,
    generate_task_generation_id,
    generate_task_id,
    generate_workflow_id,
    generate_workflow_parameter_id,
    generate_workflow_permanent_id,
    generate_workflow_run_id,
)
from skyvern.forge.sdk.schemas.tasks import ProxyLocation


class Base(AsyncAttrs, DeclarativeBase):
    pass


class TaskModel(Base):
    __tablename__ = "tasks"

    task_id = Column(String, primary_key=True, index=True, default=generate_task_id)
    organization_id = Column(String, ForeignKey("organizations.organization_id"))
    status = Column(String, index=True)
    webhook_callback_url = Column(String)
    title = Column(String)
    url = Column(String)
    navigation_goal = Column(String)
    data_extraction_goal = Column(String)
    navigation_payload = Column(JSON)
    extracted_information = Column(JSON)
    failure_reason = Column(String)
    proxy_location = Column(Enum(ProxyLocation))
    extracted_information_schema = Column(JSON)
    workflow_run_id = Column(String, ForeignKey("workflow_runs.workflow_run_id"))
    order = Column(Integer, nullable=True)
    retry = Column(Integer, nullable=True)
    error_code_mapping = Column(JSON, nullable=True)
    errors = Column(JSON, default=[], nullable=False)
    max_steps_per_run = Column(Integer, nullable=True)
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
    __table_args__ = (Index("org_task_index", "organization_id", "task_id"),)

    step_id = Column(String, primary_key=True, index=True, default=generate_step_id)
    organization_id = Column(String, ForeignKey("organizations.organization_id"))
    task_id = Column(String, ForeignKey("tasks.task_id"))
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
    task_id = Column(String, ForeignKey("tasks.task_id"))
    step_id = Column(String, ForeignKey("steps.step_id"))
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
    )

    workflow_id = Column(String, primary_key=True, index=True, default=generate_workflow_id)
    organization_id = Column(String, ForeignKey("organizations.organization_id"))
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    workflow_definition = Column(JSON, nullable=False)
    proxy_location = Column(Enum(ProxyLocation))
    webhook_callback_url = Column(String)

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

    workflow_run_id = Column(String, primary_key=True, index=True, default=generate_workflow_run_id)
    workflow_id = Column(String, ForeignKey("workflows.workflow_id"), nullable=False)
    status = Column(String, nullable=False)
    proxy_location = Column(Enum(ProxyLocation))
    webhook_callback_url = Column(String)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
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
    url_parameter_key = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
    )
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
    user_prompt = Column(String, nullable=False, index=True)  # The prompt from the user
    url = Column(String)
    navigation_goal = Column(String)
    navigation_payload = Column(JSON)
    data_extraction_goal = Column(String)
    extracted_information_schema = Column(JSON)

    llm = Column(String)  # language model to use
    llm_prompt = Column(String)  # The prompt sent to the language model
    llm_response = Column(String)  # The response from the language model

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)
