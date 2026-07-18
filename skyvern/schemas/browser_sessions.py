from pydantic import BaseModel, Field, field_validator

from skyvern.client.types.workflow_definition_yaml_blocks_item import WorkflowDefinitionYamlBlocksItem
from skyvern.client.types.workflow_definition_yaml_parameters_item import WorkflowDefinitionYamlParametersItem_Workflow
from skyvern.forge.sdk.schemas.persistent_browser_sessions import Extensions, PersistentBrowserType
from skyvern.schemas.docs.doc_strings import PROXY_LOCATION_DOC_STRING
from skyvern.schemas.proxy_pinning import validate_proxy_session_id
from skyvern.schemas.runs import GeoTarget, ProxyLocationInput
from skyvern.services.browser_recording.types import RecordingDraftStep

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
    proxy_location: ProxyLocationInput = Field(
        default=None,
        description=PROXY_LOCATION_DOC_STRING + " Can also be a GeoTarget object for granular city/state targeting: "
        '{"country": "US", "subdivision": "CA", "city": "San Francisco"}, '
        "or a custom proxy URL dict for self-hosted deployments: "
        '{"url": "http://user:password@proxy.example.com:8080"}',
    )

    @field_validator("proxy_location", mode="before")
    @classmethod
    def validate_proxy_location_dict(cls, proxy_location: object) -> object:
        if isinstance(proxy_location, dict):
            # Custom proxy URL dict: {"url": "http://..."} — pass through for self-hosted deployments.
            if "url" in proxy_location and "country" not in proxy_location:
                return proxy_location
            return GeoTarget.model_validate(proxy_location)
        return proxy_location

    proxy_session_id: str | None = Field(
        default=None,
        description="Opaque Skyvern-managed proxy sticky-session id for pinned Residential ISP sessions.",
    )

    @field_validator("proxy_session_id")
    @classmethod
    def validate_proxy_session_id_field(cls, value: str | None) -> str | None:
        return validate_proxy_session_id(value)

    extensions: list[Extensions] | None = Field(
        default=None,
        description="A list of extensions to install in the browser session.",
    )

    browser_type: PersistentBrowserType | None = Field(
        default=None,
        description="The type of browser to use for the session.",
    )

    browser_profile_id: str | None = Field(
        default=None,
        description="ID of a browser profile to load into this session (restores cookies, localStorage, etc.). browser_profile_id starts with `bp_`.",
        pattern=r"^bp_",
    )

    generate_browser_profile: bool = Field(
        default=False,
        description="When true, the session's browser profile (cookies, localStorage, etc.) is saved to storage "
        "when the session ends so it can be turned into a reusable browser profile. Defaults to false to avoid "
        "storing profiles for sessions that never need them. Sessions started with a browser_profile_id always "
        "persist their profile regardless of this flag.",
    )


class UpdateBrowserSessionRequest(BaseModel):
    generate_browser_profile: bool = Field(
        description="Enable or disable saving this session's browser profile when it ends. Can be toggled while "
        "the session is still alive; the value is read at session teardown.",
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
    draft_steps: list[RecordingDraftStep] | None = Field(
        default=None,
        description="Optional live interpretation drafts to commit instead of reprocessing the compressed recording.",
    )
    code_first: bool = Field(
        default=False,
        description="When true, synthesize deterministic code blocks from the recording instead of agent blocks.",
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
