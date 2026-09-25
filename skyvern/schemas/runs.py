from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, TypeAlias, Union

from pydantic import (
    AliasChoices,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    WebsocketUrl,
    field_serializer,
    field_validator,
    model_validator,
)

from skyvern.forge.sdk.db.enums import BrowserSeedSource, WorkflowRunTriggerType
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.models.run_limits import (
    WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES,
    WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES,
    MaxScreenshotScrolls,
    reject_bool_max_elapsed_time_minutes,
)
from skyvern.forge.sdk.workflow.models.validators import (
    normalize_run_metadata,
    normalize_run_with,
)
from skyvern.schemas.docs.doc_examples import (
    BROWSER_SESSION_ID_EXAMPLES,
    ERROR_CODE_MAPPING_EXAMPLES,
    TASK_PROMPT_EXAMPLES,
    TASK_URL_EXAMPLES,
    TOTP_IDENTIFIER_EXAMPLES,
    TOTP_URL_EXAMPLES,
)
from skyvern.schemas.docs.doc_strings import (
    BROWSER_SESSION_ID_DOC_STRING,
    DATA_EXTRACTION_SCHEMA_DOC_STRING,
    ERROR_CODE_MAPPING_DOC_STRING,
    MAX_STEPS_DOC_STRING,
    MODEL_CONFIG,
    PROXY_LOCATION_DOC_STRING,
    RUN_FILE_IDS_DOC_STRING,
    TASK_ENGINE_DOC_STRING,
    TASK_PROMPT_DOC_STRING,
    TASK_URL_DOC_STRING,
    TOTP_IDENTIFIER_DOC_STRING,
    TOTP_URL_DOC_STRING,
    WEBHOOK_URL_DOC_STRING,
)
from skyvern.schemas.proxy_location import (  # noqa: F401
    SUPPORTED_GEO_COUNTRIES,
    GeoTarget,
    ProxyLocation,
    ProxyLocationInput,
    get_tzinfo_from_proxy,
    proxy_location_to_request,
)
from skyvern.schemas.run_enums import (  # noqa: F401
    CUA_ENGINES,
    CUA_RUN_TYPES,
    TERMINAL_STATUSES,
    RunEngine,
    RunStatus,
    RunType,
)
from skyvern.utils.secret_headers import mask_header_values
from skyvern.utils.url_validators import WebhookUrl, validate_browser_host, validate_url


class BrowserType(StrEnum):
    """Browser engine selectable at the workflow and workflow-run level.

    This is the workflow/run feature's own domain truth for browser selection. It is deliberately a
    distinct enum from the PBS ``PersistentBrowserType`` (which stays dedicated to persistent-session
    concepts) — not an alias or subclass. The concrete serialized values are intentionally shared
    with PBS (a contract test enforces value parity), and the cloud runtime maps them by value
    through ``CloudBrowserType.from_source_browser_type``.
    """

    MSEdge = "msedge"
    Chrome = "chrome"
    StealthChromium = "stealth-chromium"


# Human labels for the pickers. A value missing here still surfaces (with a derived label), so a
# newly added BrowserType needs no edit to appear — only an optional nicer label.
_BROWSER_TYPE_LABELS: dict[str, str] = {
    BrowserType.MSEdge.value: "Microsoft Edge",
    BrowserType.Chrome.value: "Google Chrome",
    BrowserType.StealthChromium.value: "Stealth Chromium",
}


class SupportsBrowserType(Protocol):
    """A workflow/run/request model (or its ORM row) that carries a ``browser_type`` selection."""

    browser_type: str | None


def read_browser_type(source: SupportsBrowserType) -> str | None:
    """Null/type-safe read of a ``browser_type`` selection from a model-like object.

    An absent attribute, or a non-string value, reads as ``None``; a real string — valid or not — is
    returned unchanged so the normal BrowserType validator / fail-fast still governs it downstream.
    """
    value = getattr(source, "browser_type", None)
    return value if isinstance(value, str) else None


def normalize_browser_type(v: str | None) -> str | None:
    """Validate a user-supplied workflow/run browser_type against the ``BrowserType`` domain enum.

    None passes through (inherit workflow / system default). An unrecognized value raises so the
    public API rejects it. ``BrowserType`` is the workflow/run source of truth (distinct from PBS's
    PersistentBrowserType).
    """
    if v is None:
        return None
    try:
        return BrowserType(v).value
    except ValueError:
        valid = ", ".join(t.value for t in BrowserType)
        raise ValueError(f"Invalid browser_type '{v}'; must be one of: {valid}")


def _default_browser_type_label(value: str) -> str:
    return value.replace("-", " ").replace("_", " ").title()


# One shared contract for both request models: an attached browser (a live session or a remote
# browser_address) already owns its engine, so a browser_type selection alongside it is a conflict.
# A browser_profile_id is intentionally still allowed with browser_type (the selection overrides the
# profile-derived engine).
BROWSER_TYPE_ATTACH_CONFLICT_MESSAGE = (
    "browser_type cannot be combined with browser_session_id or browser_address — an attached browser "
    "owns its engine. Remove the browser_type selection, or drop the session/address to pick an engine."
)


def browser_type_attach_conflict(
    *, browser_type: str | None, browser_session_id: str | None, browser_address: str | None
) -> bool:
    """A non-null browser_type combined with an attached browser (session or remote address) is a
    conflict. browser_profile_id is deliberately not an attachment here."""
    return browser_type is not None and (browser_session_id is not None or browser_address is not None)


class BrowserTypeOption(BaseModel):
    value: str
    label: str


def supported_browser_type_options() -> list[BrowserTypeOption]:
    """Selectable browser engines for the workflow/run browser_type setting, generated from the
    ``BrowserType`` enum so a new engine surfaces to validation, this options list, and the UI
    without separate option maintenance."""
    return [
        BrowserTypeOption(value=t.value, label=_BROWSER_TYPE_LABELS.get(t.value, _default_browser_type_label(t.value)))
        for t in BrowserType
    ]


MAX_SEARCH_FETCH_LIMIT = 1000
MAX_RUN_ATTACHED_FILES = 50
_BROWSER_ADDRESS_ADAPTER = TypeAdapter(AnyHttpUrl | WebsocketUrl)
BROWSER_ADDRESS_SERVER_ASSIGNED_CONTEXT_KEY = "browser_address_is_server_assigned"
BROWSER_SESSION_SERVER_ASSIGNED_CONTEXT_KEY = "browser_session_id_is_server_assigned"

# Type checkers need string Literal values, while pydantic's discriminated
# union preserves enum instances when runtime Literals use the enum members.
if TYPE_CHECKING:
    TaskRunTypeField: TypeAlias = Literal[
        "task_v1", "task_v2", "task_v3", "openai_cua", "anthropic_cua", "ui_tars", "yutori_navigator"
    ]
    WorkflowRunTypeField: TypeAlias = Literal["workflow_run"]
else:
    TaskRunTypeField = Literal[
        RunType.task_v1,
        RunType.task_v2,
        RunType.task_v3,
        RunType.openai_cua,
        RunType.anthropic_cua,
        RunType.ui_tars,
        RunType.yutori_navigator,
    ]
    WorkflowRunTypeField = Literal[RunType.workflow_run]


def _validate_browser_address(browser_address: str | None) -> str | None:
    if not browser_address:
        return browser_address

    try:
        parsed = _BROWSER_ADDRESS_ADAPTER.validate_python(browser_address)
    except ValidationError as exc:
        raise ValueError("browser_address must be an HTTP(S) or WebSocket URL with a host") from exc

    if not parsed.host:
        raise ValueError("browser_address must include a host")

    validate_browser_host(parsed.host)
    return browser_address


def _browser_address_is_server_assigned(info: ValidationInfo) -> bool:
    return bool(info.context and info.context.get(BROWSER_ADDRESS_SERVER_ASSIGNED_CONTEXT_KEY))


def _browser_session_is_server_assigned(info: ValidationInfo) -> bool:
    return bool(info.context and info.context.get(BROWSER_SESSION_SERVER_ASSIGNED_CONTEXT_KEY))


class TaskRunRequest(BaseModel):
    prompt: str = Field(
        description=TASK_PROMPT_DOC_STRING,
        examples=TASK_PROMPT_EXAMPLES,
    )
    url: str | None = Field(
        default=None,
        description=TASK_URL_DOC_STRING,
        examples=TASK_URL_EXAMPLES,
    )
    engine: RunEngine = Field(
        default=RunEngine.skyvern_v1,
        description=TASK_ENGINE_DOC_STRING,
    )
    title: str | None = Field(
        default=None, description="The title for the task", examples=["The title of my first skyvern task"]
    )
    proxy_location: ProxyLocationInput = Field(
        default=ProxyLocation.RESIDENTIAL,
        description=PROXY_LOCATION_DOC_STRING + " Can also be a GeoTarget object for granular city/state targeting: "
        '{"country": "US", "subdivision": "CA", "city": "San Francisco"}',
    )
    data_extraction_schema: dict | list | str | None = Field(
        default=None,
        description=DATA_EXTRACTION_SCHEMA_DOC_STRING,
    )
    error_code_mapping: dict[str, str] | None = Field(
        default=None,
        description=ERROR_CODE_MAPPING_DOC_STRING,
        examples=ERROR_CODE_MAPPING_EXAMPLES,
    )
    max_steps: int | None = Field(
        default=None,
        description=MAX_STEPS_DOC_STRING,
        examples=[10, 25],
    )
    webhook_url: str | None = Field(
        default=None,
        description=WEBHOOK_URL_DOC_STRING,
        examples=["https://my-site.com/webhook"],
    )
    totp_identifier: str | None = Field(
        default=None,
        description=TOTP_IDENTIFIER_DOC_STRING,
        examples=TOTP_IDENTIFIER_EXAMPLES,
    )
    totp_url: str | None = Field(
        default=None,
        description=TOTP_URL_DOC_STRING,
        examples=TOTP_URL_EXAMPLES,
    )
    browser_session_id: str | None = Field(
        default=None,
        description=BROWSER_SESSION_ID_DOC_STRING,
        examples=BROWSER_SESSION_ID_EXAMPLES,
    )
    browser_profile_id: str | None = Field(
        default=None,
        description="ID of a browser profile to reuse for this task",
    )
    start_fresh_browser: bool = Field(
        default=False,
        description=(
            "When true, start this run from a fresh, empty browser and ignore any saved browser "
            "memory — no memory is read or written. A verified sign-in during the run still updates "
            "the credential's saved login."
        ),
    )
    model: dict[str, Any] | None = Field(
        default=None,
        description=MODEL_CONFIG,
        examples=None,
    )
    extra_http_headers: dict[str, str] | None = Field(
        default=None,
        description="The extra HTTP headers for the requests in browser.",
    )
    cdp_connect_headers: dict[str, str] | None = Field(
        default=None,
        description=(
            "HTTP headers attached ONLY to the CDP WebSocket handshake when connecting to "
            "a remote browser via browser_address. Use this for browser-provider auth "
            "(e.g., x-api-key for Skyvern Cloud, Browserless, or similar). These headers "
            "are NEVER forwarded to target websites."
        ),
    )
    publish_workflow: bool = Field(
        default=False,
        description=(
            "Deprecated. Whether to publish a `skyvern-2.0` task as a reusable workflow. "
            "For backwards compatibility, this routes the request through the legacy `skyvern-2.0` "
            "publish path. Prefer creating reusable workflows through the workflow APIs."
        ),
        json_schema_extra={"deprecated": True},
    )
    include_action_history_in_verification: bool | None = Field(
        default=False, description="Whether to include action history when verifying that the task is complete"
    )
    max_screenshot_scrolls: MaxScreenshotScrolls = Field(
        default=None,
        description="The maximum number of scrolls for the post action screenshot. When it's None or 0, it takes the current viewpoint screenshot.",
    )
    browser_address: str | None = Field(
        default=None,
        description="The CDP address for the task.",
        examples=["http://127.0.0.1:9222", "ws://127.0.0.1:9222/devtools/browser/1234567890"],
    )
    run_with: str | None = Field(
        default=None,
        description="Whether to run the task with agent or code. Null means use the default.",
        examples=["agent", "code"],
    )
    file_ids: list[str] | None = Field(
        default=None,
        max_length=MAX_RUN_ATTACHED_FILES,
        description=RUN_FILE_IDS_DOC_STRING,
        examples=[["file_123456789"]],
    )

    @field_validator("run_with", mode="before")
    @classmethod
    def _normalize_run_with(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return normalize_run_with(v)

    @field_validator("url")
    @classmethod
    def validate_task_url(cls, url: str | None) -> str | None:
        if not url:
            return url
        return validate_url(url)

    @field_validator("browser_address")
    @classmethod
    def validate_browser_address(cls, browser_address: str | None) -> str | None:
        return _validate_browser_address(browser_address)

    @field_validator("webhook_url", "totp_url")
    @classmethod
    def validate_callback_urls(cls, url: str | None, info: ValidationInfo) -> str | None:
        """
        Validates that URLs provided to Skyvern are properly formatted.

        Args:
            url: The URL for Skyvern to validate

        Returns:
            The validated URL or None if no URL was provided
        """
        if not url:
            return url

        return validate_url(url, field_name=info.field_name or "url")

    @field_serializer("cdp_connect_headers")
    def _mask_cdp_connect_headers(self, headers: dict[str, str] | None) -> dict[str, str] | None:
        return mask_header_values(headers)

    @model_validator(mode="after")
    def _route_publish_workflow_to_v2(self) -> TaskRunRequest:
        if self.publish_workflow and self.engine != RunEngine.skyvern_v2:
            self.engine = RunEngine.skyvern_v2
        return self

    @model_validator(mode="after")
    def _reject_start_fresh_with_session(self) -> TaskRunRequest:
        if self.start_fresh_browser and self.browser_session_id:
            raise ValueError(
                "start_fresh_browser cannot be combined with browser_session_id — "
                "a live session is the browser for the run."
            )
        return self

    @model_validator(mode="after")
    def _reject_start_fresh_with_profile(self) -> TaskRunRequest:
        if self.start_fresh_browser and self.browser_profile_id:
            raise ValueError(
                "start_fresh_browser cannot be combined with browser_profile_id — "
                "pick one: a fresh browser or a specific profile."
            )
        return self

    @model_validator(mode="after")
    def _reject_start_fresh_with_address(self) -> TaskRunRequest:
        if self.start_fresh_browser and self.browser_address:
            raise ValueError(
                "start_fresh_browser cannot be combined with browser_address — "
                "connecting to an existing remote browser reuses its session state."
            )
        return self


class WorkflowRunRequest(BaseModel):
    # An agent *is* a workflow, so `agent_id` and `workflow_id` carry the same `wpid_` value. `agent_id`
    # is listed first so the public request schema advertises the agent-named field, while `workflow_id`
    # stays accepted for backwards compatibility. `populate_by_name` keeps internal
    # `WorkflowRunRequest(workflow_id=...)` construction working alongside the aliases.
    model_config = ConfigDict(populate_by_name=True)

    workflow_id: str = Field(
        validation_alias=AliasChoices("agent_id", "workflow_id"),
        description="ID of the agent to run. Starts with `wpid_`. `workflow_id` is accepted as an alias.",
        examples=["wpid_123"],
    )
    parameters: dict[str, Any] | None = Field(default=None, description="Parameters to pass to the workflow")
    title: str | None = Field(default=None, description="The title for this workflow run")
    proxy_location: ProxyLocationInput = Field(
        default=ProxyLocation.RESIDENTIAL,
        description=PROXY_LOCATION_DOC_STRING + " Can also be a GeoTarget object for granular city/state targeting: "
        '{"country": "US", "subdivision": "CA", "city": "San Francisco"}',
    )
    webhook_url: str | None = Field(
        default=None,
        description="URL to send workflow status updates to after a run is finished. Refer to https://www.skyvern.com/docs/running-tasks/webhooks-faq for webhook questions.",
    )
    totp_url: str | None = Field(
        default=None,
        description=TOTP_URL_DOC_STRING,
        examples=TOTP_URL_EXAMPLES,
    )
    totp_identifier: str | None = Field(
        default=None,
        description=TOTP_IDENTIFIER_DOC_STRING,
        examples=TOTP_IDENTIFIER_EXAMPLES,
    )
    browser_session_id: str | None = Field(
        default=None,
        description="ID of a Skyvern browser session to reuse, having it continue from the current screen state",
    )
    reuse_browser_session: bool | None = Field(
        default=None,
        description="Override whether this run reuses the workflow's managed browser session. Null inherits the workflow setting. Without login credentials, a browser profile key, or a sequential key, reuse is workflow-scoped: every run shares one browser and its signed-in state, so treat the workflow as single-account.",
    )
    browser_profile_id: str | None = Field(
        default=None,
        description="ID of a browser profile to reuse for this workflow run",
    )
    start_fresh_browser: bool = Field(
        default=False,
        description=(
            "When true, start this run from a fresh, empty browser and ignore any saved browser "
            "memory — no memory is read or written. A verified sign-in during the run still updates "
            "the credential's saved login."
        ),
    )
    max_screenshot_scrolls: MaxScreenshotScrolls = Field(
        default=None,
        description="The maximum number of scrolls for the post action screenshot. When it's None or 0, it takes the current viewpoint screenshot.",
    )
    max_elapsed_time_minutes: int | None = Field(
        default=None,
        ge=1,
        le=WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES,
        description=(
            "Timeout this workflow run after the configured elapsed runtime in minutes. "
            f"When omitted, the platform default is {WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES} minutes. "
            f"The maximum configurable value is {WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES} minutes."
        ),
    )
    extra_http_headers: dict[str, str] | None = Field(
        default=None,
        description="The extra HTTP headers for the requests in browser.",
    )
    cdp_connect_headers: dict[str, str] | None = Field(
        default=None,
        description=(
            "HTTP headers attached ONLY to the CDP WebSocket handshake when connecting to "
            "a remote browser via browser_address. Use this for browser-provider auth "
            "(e.g., x-api-key for Skyvern Cloud, Browserless, or similar). These headers "
            "are NEVER forwarded to target websites."
        ),
    )
    browser_address: str | None = Field(
        default=None,
        description="The CDP address for the workflow run.",
        examples=["http://127.0.0.1:9222", "ws://127.0.0.1:9222/devtools/browser/1234567890"],
    )
    ai_fallback: bool | None = Field(
        default=None,
        description="Whether to fallback to AI if the workflow run fails.",
    )
    run_with: str | None = Field(
        default=None,
        description="Whether to run the workflow with agent or code. Null inherits from the workflow setting.",
        examples=["agent", "code"],
    )
    browser_type: str | None = Field(
        default=None,
        description="Browser engine for this run, one of the supported browser types "
        "(e.g. msedge, chrome, stealth-chromium). Overrides the workflow-level setting. "
        "Null inherits from the workflow.",
        examples=["chrome"],
    )
    run_metadata: dict[str, str] | None = Field(
        default=None,
        description="String key/value metadata to attach to this workflow run for analytics tag filtering.",
    )
    file_ids: list[str] | None = Field(
        default=None,
        max_length=MAX_RUN_ATTACHED_FILES,
        description=RUN_FILE_IDS_DOC_STRING,
        examples=[["file_123456789"]],
    )

    @field_validator("run_with", mode="before")
    @classmethod
    def _normalize_run_with(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return normalize_run_with(v)

    @field_validator("browser_type", mode="before")
    @classmethod
    def _normalize_browser_type(cls, v: str | None) -> str | None:
        return normalize_browser_type(v)

    @field_validator("max_elapsed_time_minutes", mode="before")
    @classmethod
    def validate_max_elapsed_time_minutes(cls, value: object) -> object:
        return reject_bool_max_elapsed_time_minutes(value)

    @field_validator("run_metadata")
    @classmethod
    def _validate_run_metadata(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        return normalize_run_metadata(v)

    @field_validator("browser_address")
    @classmethod
    def validate_browser_address(cls, browser_address: str | None, info: ValidationInfo) -> str | None:
        if _browser_address_is_server_assigned(info):
            return browser_address
        return _validate_browser_address(browser_address)

    @field_validator("webhook_url", "totp_url")
    @classmethod
    def validate_urls(cls, url: str | None, info: ValidationInfo) -> str | None:
        if not url:
            return url
        return validate_url(url, field_name=info.field_name or "url")

    @field_serializer("cdp_connect_headers")
    def _mask_cdp_connect_headers(self, headers: dict[str, str] | None) -> dict[str, str] | None:
        return mask_header_values(headers)

    @model_validator(mode="after")
    def _reject_browser_type_with_attached_browser(self, info: ValidationInfo) -> WorkflowRunRequest:
        if not browser_type_attach_conflict(
            browser_type=self.browser_type,
            browser_session_id=self.browser_session_id,
            browser_address=self.browser_address,
        ):
            return self
        # A raw browser_type+attachment conflict exists. Reconstruction re-materializes the server's own
        # persisted state (a typed run can legitimately gain a server-generated session via reuse /
        # FORCE_BROWSER_SESSION / human-interaction), so a SERVER-ASSIGNED session/address is excused;
        # a caller-supplied one (no context) still 422s at ingress.
        session_ok = self.browser_session_id is None or _browser_session_is_server_assigned(info)
        address_ok = self.browser_address is None or _browser_address_is_server_assigned(info)
        if session_ok and address_ok:
            return self
        raise ValueError(BROWSER_TYPE_ATTACH_CONFLICT_MESSAGE)

    @model_validator(mode="after")
    def _reject_start_fresh_with_session(self) -> WorkflowRunRequest:
        if self.start_fresh_browser and self.browser_session_id:
            raise ValueError(
                "start_fresh_browser cannot be combined with browser_session_id — "
                "a live session is the browser for the run."
            )
        return self

    @model_validator(mode="after")
    def _reject_start_fresh_with_profile(self) -> WorkflowRunRequest:
        if self.start_fresh_browser and self.browser_profile_id:
            raise ValueError(
                "start_fresh_browser cannot be combined with browser_profile_id — "
                "pick one: a fresh browser or a specific profile."
            )
        return self

    @model_validator(mode="after")
    def _reject_start_fresh_with_address(self) -> WorkflowRunRequest:
        if self.start_fresh_browser and self.browser_address:
            raise ValueError(
                "start_fresh_browser cannot be combined with browser_address — "
                "connecting to an existing remote browser reuses its session state."
            )
        return self


class BlockRunRequest(WorkflowRunRequest):
    webhook_url: WebhookUrl | None = Field(
        default=None,
        description="URL to send workflow status updates to after the run finishes.",
    )
    block_labels: list[str] = Field(
        description="Labels of the blocks to execute",
        examples=["block_1", "block_2"],
    )
    block_outputs: dict[str, Any] | None = Field(
        default=None,
        # NOTE(jdo): this is either the last output of the block for a given
        # org_id/user_id, or an override supplied by the user
        description="Any active outputs of blocks in a workflow being debugged",
    )
    code_gen: bool | None = Field(
        default=False,
        description="Whether to generate colde for blocks that support it",
    )
    debug_session_id: str | None = Field(
        default=None,
        description="ID of the debug session to use for this block run",
    )


def should_suppress_memory_write(start_fresh_browser: bool | None) -> bool:
    # Governs own-memory / healthy-run write-back only. Credential banking is unaffected
    # (a verified fresh sign-in still banks), so do not consult this at the credential-write path.
    return bool(start_fresh_browser)


def resolve_start_fresh(start_fresh_browser: bool | None, override_browser_profile_id: str | None) -> bool:
    # An explicit per-run browser_profile_id override wins over the fresh flag (the flag suppresses
    # only BELOW the run-override level). The seed resolver ranks a raw start_fresh above the override,
    # so this gate is the deliberate reconciliation of the two layers.
    return bool(start_fresh_browser) and not override_browser_profile_id


class ScriptRunResponse(BaseModel):
    # `extra="ignore"` is the Pydantic v2 default; making it explicit
    # pins the forward-compat guarantee (unknown keys silently dropped).
    model_config = ConfigDict(extra="ignore")

    # True iff a fallback fired during this run, flipping at least one
    # block's execution from cached script to the agent. Writers: the two
    # `services/script_service.py` fallback paths (script-block failure +
    # conditional-agent episode) and the `_execute_single_block` script-
    # failure path. `False` here does NOT imply "no AI execution" — blocks
    # that were ALWAYS-agent (via `requires_agent`, `disable_cache`, or
    # non-cacheable block types) never create a fallback episode and don't
    # flip this flag. For per-block routing ground truth, consult the
    # `Block execution mode resolved` log emitted at per-block execution
    # time in `skyvern/forge/sdk/workflow/service.py`.
    ai_fallback_triggered: bool = False

    # Identity of the cached script that was loaded for this run at
    # workflow setup time. Non-null iff a script was loaded. Does NOT
    # imply that every (or any) block actually executed from that cache —
    # per-block `block_labels` filtering, `requires_agent`, `disable_cache`,
    # or non-cacheable block types can still route individual blocks to AI.
    # Populated by the server-side execution path (workflow/service.py) and
    # the local CLI entrypoint (services/script_service.run_script). None
    # on rows written by older code paths that only recorded
    # `ai_fallback_triggered`.
    script_id: str | None = None
    script_revision_id: str | None = None


class UploadFileResponse(BaseModel):
    s3_uri: str = Field(description="S3 URI where the file was uploaded")
    presigned_url: str = Field(description="Presigned URL to access the uploaded file")
    file_id: str | None = Field(
        default=None,
        description="Identifier for this upload. Pass it to DELETE /v1/files/{file_id} to delete the file.",
    )
    expires_at: datetime | None = Field(
        default=None,
        description=(
            "When the file will be deleted, if a retention_days was supplied. "
            "Null means the file has no expiry of its own and follows the organization's data retention policy."
        ),
    )


class BaseRunResponse(BaseModel):
    run_id: str = Field(
        description="Unique identifier for this run. Run ID starts with `tsk_` for task runs and `wr_` for workflow runs.",
        examples=["tsk_123", "tsk_v2_123", "wr_123"],
    )
    status: RunStatus = Field(
        description="Current status of the run",
        examples=[
            "created",
            "queued",
            "running",
            "paused",
            "timed_out",
            "failed",
            "terminated",
            "completed",
            "canceled",
        ],
    )
    output: dict | list | str | None = Field(
        default=None,
        description="Output data from the run, if any. Format/schema depends on the data extracted by the run.",
    )
    downloaded_files: list[FileInfo] | None = Field(default=None, description="List of files downloaded during the run")
    recording_url: str | None = Field(default=None, description="URL to the recording of the run")
    recording_archived: bool = Field(
        default=False,
        description="True when the recording exists but has been archived to cold storage and is not currently accessible.",
    )
    screenshot_urls: list[str] | None = Field(
        default=None,
        description="List of last n screenshot URLs in reverse chronological order - the first one the list is the latest screenshot.",
    )
    failure_reason: str | None = Field(default=None, description="Reason for failure if the run failed or terminated")
    created_at: datetime = Field(description="Timestamp when this run was created", examples=["2025-01-01T00:00:00Z"])
    modified_at: datetime = Field(
        description="Timestamp when this run was last modified", examples=["2025-01-01T00:05:00Z"]
    )
    queued_at: datetime | None = Field(default=None, description="Timestamp when this run was queued")
    started_at: datetime | None = Field(default=None, description="Timestamp when this run started execution")
    finished_at: datetime | None = Field(default=None, description="Timestamp when this run finished")
    app_url: str | None = Field(
        default=None,
        description="URL to the application UI where the run can be viewed",
        examples=["https://app.skyvern.com/tasks/tsk_123", "https://app.skyvern.com/workflows/wpid_123/wr_123"],
    )
    browser_session_id: str | None = Field(
        default=None, description="ID of the Skyvern persistent browser session used for this run", examples=["pbs_123"]
    )
    browser_profile_id: str | None = Field(
        default=None,
        description="ID of the browser profile used for this run",
        examples=["bp_123"],
    )
    max_screenshot_scrolls: int | None = Field(
        default=None,
        description="The maximum number of scrolls for the post action screenshot. When it's None or 0, it takes the current viewpoint screenshot",
    )
    script_run: ScriptRunResponse | None = Field(
        default=None,
        description="The script run result",
    )
    errors: list[dict[str, Any]] | None = Field(
        default=None,
        description="The errors for the run",
    )
    step_count: int | None = Field(
        default=None,
        description="Total number of steps executed in this run",
    )


class WorkflowRunAttempt(BaseModel):
    attempt_number: int = Field(description="One-based number of this workflow run attempt")
    status: RunStatus = Field(description="Status of this workflow run attempt")
    failure_reason: str | None = Field(default=None, description="Reason for failure, if the attempt failed")
    error_codes: list[str] = Field(default_factory=list, description="Error codes reported by this attempt")
    started_at: datetime | None = Field(default=None, description="Timestamp when this attempt started")
    finished_at: datetime | None = Field(default=None, description="Timestamp when this attempt finished")
    retry_decision: str | None = Field(default=None, description="Retry decision recorded for this attempt")
    decision_reason: str | None = Field(default=None, description="Reason recorded for the retry decision")
    next_attempt_at: datetime | None = Field(
        default=None,
        description="Timestamp when the next attempt is scheduled",
    )
    webhook_sent_at: datetime | None = Field(
        default=None,
        description="Timestamp when the webhook for this attempt was sent",
    )


class TaskRunResponse(BaseRunResponse):
    run_type: TaskRunTypeField = Field(
        description="Types of a task run - task_v1, task_v2, openai_cua, anthropic_cua, ui_tars"
    )
    run_request: TaskRunRequest | None = Field(
        default=None, description="The original request parameters used to start this task run"
    )


class WorkflowRunResponse(BaseRunResponse):
    run_type: WorkflowRunTypeField = Field(description="Type of run - always workflow_run for workflow runs")
    attempt: int = Field(default=1, description="One-based number of the current workflow run attempt")
    retry_pending: bool = Field(
        default=False,
        description="Whether another attempt is scheduled for this workflow run",
    )
    next_attempt_at: datetime | None = Field(
        default=None,
        description="Timestamp when the next workflow run attempt is scheduled",
    )
    attempts: list[WorkflowRunAttempt] = Field(
        default_factory=list,
        description="Attempts recorded for this workflow run",
    )
    run_with: str = Field(
        default="agent",
        description="Whether the workflow run was executed with agent or code",
        examples=["agent", "code"],
    )

    @field_validator("run_with", mode="before")
    @classmethod
    def _normalize_run_with(cls, v: str | None) -> str:
        return normalize_run_with(v)

    ai_fallback: bool | None = Field(
        default=None,
        description="Whether to fallback to AI if code run fails.",
    )
    script_id: str | None = Field(
        default=None,
        description="ID of the cached script used for this workflow run, if any.",
    )
    browser_seed_source: BrowserSeedSource | None = Field(
        default=None,
        description="Which layer of the seed-precedence chain seeded this run's browser (provenance).",
        examples=["credential", "own_memory", "fresh"],
    )
    run_request: WorkflowRunRequest | None = Field(
        default=None, description="The original request parameters used to start this workflow run"
    )
    # NOTE: no top-level `agent_id` here on purpose. This model has no reliable permanent-id field
    # (only `run_request.workflow_id`, which is absent on some reads and is a version id on the
    # login/download paths), so a computed alias would echo null/wrong values. The reliable agent_id
    # alias lives on WorkflowRunResponseBase / Workflow / RunWorkflowResponse instead.


RunResponse = Annotated[Union[TaskRunResponse, WorkflowRunResponse], Field(discriminator="run_type")]


class BlockRunResponse(WorkflowRunResponse):
    block_labels: list[str] = Field(description="A whitelist of block labels; only these blocks will execute")


class TaskRunListItem(BaseModel):
    """Lightweight run-history item backed by the task_runs table."""

    model_config = ConfigDict(from_attributes=True)

    task_run_id: str
    run_id: str
    task_run_type: str
    status: str
    title: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    workflow_permanent_id: str | None = None
    workflow_deleted: bool = False
    script_run: bool = False
    trigger_type: WorkflowRunTriggerType | None = None
    searchable_text: str | None = Field(default=None, exclude=True)

    @field_validator("script_run", mode="before")
    @classmethod
    def coerce_script_run(cls, v: Any) -> bool:
        """Intentionally lossy: collapse dict metadata / bool / None → bool for the list view.

        The full script execution metadata (dict) is available via the detail
        endpoint's Run.script_run field.  Do not rely on dict contents here.
        """
        return bool(v)


class BulkCancelRunsRequest(BaseModel):
    run_ids: list[str] = Field(max_length=100, description="List of run IDs to cancel")


class BulkCancelRunsResponse(BaseModel):
    cancelled: list[str] = Field(description="Run IDs that were successfully cancelled")
    failed: list[str] = Field(description="Run IDs that could not be cancelled")
