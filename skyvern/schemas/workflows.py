from pydantic import BaseModel, Field

from skyvern.forge.sdk.workflow.models.yaml import WorkflowCreateYAMLRequest


class WorkflowRequest(BaseModel):
    json_definition: WorkflowCreateYAMLRequest | None = Field(
        default=None,
        description="Workflow definition in JSON format",
    )
    yaml_definition: str | None = Field(
        default=None,
        description="Workflow definition in YAML format",
    )
