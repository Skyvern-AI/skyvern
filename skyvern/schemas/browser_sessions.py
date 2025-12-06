from pydantic import BaseModel, Field

from skyvern.client.types.workflow_definition_yaml_blocks_item import WorkflowDefinitionYamlBlocksItem
from skyvern.client.types.workflow_definition_yaml_parameters_item import WorkflowDefinitionYamlParametersItem_Workflow
from skyvern.schemas.docs.doc_strings import PROXY_LOCATION_DOC_STRING
from skyvern.schemas.runs import ProxyLocation

MIN_TIMEOUT = 5
MAX_TIMEOUT = 60 * 24  # 24 hours
DEFAULT_TIMEOUT = 60


class CreateBrowserSessionRequest(BaseModel):
    timeout: int | None = Field(
        default=DEFAULT_TIMEOUT,
        description=f"Timeout in minutes for the session. Timeout is applied after the session is started. Must be between {MIN_TIMEOUT} and {MAX_TIMEOUT}. Defaults to {DEFAULT_TIMEOUT}.",
        ge=MIN_TIMEOUT,
        le=MAX_TIMEOUT,
    )
    proxy_location: ProxyLocation | None = Field(
        default=None,
        description=PROXY_LOCATION_DOC_STRING,
    )


class ProcessBrowserSessionRecordingRequest(BaseModel):
    compressed_chunks: list[str] = Field(
        default=[],
        description="List of base64 encoded and compressed (gzip) event strings representing the browser session recording.",
    )
    workflow_permanent_id: str = Field(
        default="no-such-wpid",
        description="Permanent ID of the workflow associated with the browser session recording.",
    )


class ProcessBrowserSessionRecordingResponse(BaseModel):
    blocks: list[WorkflowDefinitionYamlBlocksItem] = Field(
        default=[],
        description="List of workflow blocks generated from the processed browser session recording.",
    )
    parameters: list[WorkflowDefinitionYamlParametersItem_Workflow] = Field(
        default=[],
        description="List of workflow parameters generated from the processed browser session recording.",
    )
