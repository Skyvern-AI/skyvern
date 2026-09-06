from datetime import datetime
from enum import StrEnum
from typing import Any, List

from pydantic import (
    BaseModel,
    Field,
    ValidationInfo,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)
from typing_extensions import Self, deprecated

from skyvern.forge.sdk.db.enums import BrowserSeedSource, WorkflowRunTriggerType
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.task_v2 import TaskV2
from skyvern.forge.sdk.workflow.exceptions import (
    InvalidFinallyBlockLabel,
    NonTerminalFinallyBlock,
    WorkflowDefinitionHasDuplicateBlockLabels,
)
from skyvern.forge.sdk.workflow.models.block import BlockTypeVar, ForLoopBlock, WhileLoopBlock, get_all_blocks
from skyvern.forge.sdk.workflow.models.parameter import PARAMETER_TYPE, OutputParameter
from skyvern.forge.sdk.workflow.models.run_limits import (
    WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES,
    MaxScreenshotScrolls,
    reject_bool_max_elapsed_time_minutes,
)
from skyvern.forge.sdk.workflow.models.validators import normalize_run_metadata, normalize_run_with
from skyvern.schemas.runs import (
    BROWSER_ADDRESS_SERVER_ASSIGNED_CONTEXT_KEY,
    ProxyLocationInput,
    ScriptRunResponse,
    _validate_browser_address,
)
from skyvern.schemas.workflows import WorkflowStatus
from skyvern.utils.secret_headers import mask_header_values
from skyvern.utils.url_validators import validate_url


@deprecated("Use WorkflowRunRequest instead")
class WorkflowRequestBody(BaseModel):
    data: dict[str, Any] | None = None
    proxy_location: ProxyLocationInput = None
    webhook_callback_url: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    browser_session_id: str | None = None
    browser_profile_id: str | None = None
    start_fresh_browser: bool = False
    reuse_browser_session: bool | None = None
    max_screenshot_scrolls: MaxScreenshotScrolls = Field(default=None)
    max_elapsed_time_minutes: int | None = Field(default=None, ge=1, le=WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES)
    extra_http_headers: dict[str, str] | None = None
    cdp_connect_headers: dict[str, str] | None = None
    browser_address: str | None = None
    run_with: str | None = None
    ai_fallback: bool | None = None
    run_metadata: dict[str, str] | None = None

    @field_validator("max_elapsed_time_minutes", mode="before")
    @classmethod
    def validate_max_elapsed_time_minutes(cls, value: object) -> object:
        return reject_bool_max_elapsed_time_minutes(value)

    @field_validator("webhook_callback_url", "totp_verification_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        if not url:
            return url
        return validate_url(url)

    @field_validator("run_metadata")
    @classmethod
    def validate_run_metadata(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        return normalize_run_metadata(v)

    @field_validator("browser_address")
    @classmethod
    def validate_browser_address(cls, browser_address: str | None, info: ValidationInfo) -> str | None:
        if info.context and info.context.get(BROWSER_ADDRESS_SERVER_ASSIGNED_CONTEXT_KEY):
            return browser_address
        return _validate_browser_address(browser_address)

    @model_validator(mode="after")
    def _reject_start_fresh_with_session(self) -> Self:
        # Covers the legacy /workflows/{id}/run endpoint, which parses this body directly. Upstream
        # request models reject the combo too, so no internal construction ever sets both.
        if self.start_fresh_browser and self.browser_session_id:
            raise ValueError(
                "start_fresh_browser cannot be combined with browser_session_id — "
                "a live session is the browser for the run."
            )
        return self

    @model_validator(mode="after")
    def _reject_start_fresh_with_profile(self) -> Self:
        if self.start_fresh_browser and self.browser_profile_id:
            raise ValueError(
                "start_fresh_browser cannot be combined with browser_profile_id — "
                "pick one: a fresh browser or a specific profile."
            )
        return self

    @model_validator(mode="after")
    def _reject_start_fresh_with_address(self) -> Self:
        if self.start_fresh_browser and self.browser_address:
            raise ValueError(
                "start_fresh_browser cannot be combined with browser_address — "
                "connecting to an existing remote browser reuses its session state."
            )
        return self


@deprecated("Use WorkflowRunResponse instead")
class RunWorkflowResponse(BaseModel):
    workflow_id: str
    workflow_run_id: str

    @computed_field(description="Alias of `workflow_id` (the agent's `wpid_` permanent ID).")  # type: ignore[prop-decorator]
    @property
    def agent_id(self) -> str:
        return self.workflow_id

    @computed_field(description="Alias of `workflow_run_id`.")  # type: ignore[prop-decorator]
    @property
    def agent_run_id(self) -> str:
        return self.workflow_run_id


class WorkflowDefinition(BaseModel):
    version: int = 1
    parameters: list[PARAMETER_TYPE]
    blocks: List[BlockTypeVar]
    finally_block_label: str | None = None
    error_code_mapping: dict[str, str] | None = None
    workflow_system_prompt: str | None = None
    completion_contract: dict[str, Any] | None = Field(
        default=None,
        description="Copilot-managed: what a run of this workflow must produce, graded at run finalization. Derived from the request when a workflow is accepted; not intended to be authored by hand.",
    )

    def validate(self) -> None:
        all_labels: set[str] = set()
        duplicate_labels: set[str] = set()

        def _collect_labels(blocks: list[BlockTypeVar]) -> None:
            for block in blocks:
                if block.label in all_labels:
                    duplicate_labels.add(block.label)
                else:
                    all_labels.add(block.label)
                if isinstance(block, (ForLoopBlock, WhileLoopBlock)) and block.loop_blocks:
                    _collect_labels(block.loop_blocks)

        _collect_labels(self.blocks)

        if duplicate_labels:
            raise WorkflowDefinitionHasDuplicateBlockLabels(duplicate_labels)

        if self.finally_block_label:
            # finally_block_label must reference a top-level block
            top_level_labels = {block.label for block in self.blocks}
            if self.finally_block_label not in top_level_labels:
                raise InvalidFinallyBlockLabel(self.finally_block_label, list(top_level_labels))
            for block in self.blocks:
                if block.label == self.finally_block_label and block.next_block_label is not None:
                    raise NonTerminalFinallyBlock(self.finally_block_label)


COPILOT_TEST_WORKFLOW_CREATOR = "copilot_test"


class Workflow(BaseModel):
    workflow_id: str
    organization_id: str
    title: str
    workflow_permanent_id: str
    version: int

    @computed_field(  # type: ignore[prop-decorator]
        description="Alias of `workflow_permanent_id` — the stable agent identifier (starts with `wpid_`)."
    )
    @property
    def agent_id(self) -> str:
        return self.workflow_permanent_id

    is_saved_task: bool
    is_template: bool = False
    description: str | None = None
    workflow_definition: WorkflowDefinition
    proxy_location: ProxyLocationInput = None
    webhook_callback_url: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    persist_browser_session: bool = False
    reuse_browser_session: bool = False
    mask_secrets: bool = False
    pin_saved_session_ip: bool = False
    browser_profile_id: str | None = None
    browser_profile_key: str | None = None
    model: dict[str, Any] | None = None
    status: WorkflowStatus = WorkflowStatus.published
    max_screenshot_scrolls: int | None = None
    max_elapsed_time_minutes: int | None = None
    extra_http_headers: dict[str, str] | None = None
    cdp_connect_headers: dict[str, str] | None = None
    run_with: str = "agent"
    ai_fallback: bool = True
    cache_key: str | None = None
    adaptive_caching: bool = False
    enable_self_healing: bool = False
    code_version: int | None = None
    generate_script_on_terminal: bool = False
    run_sequentially: bool | None = None
    sequential_key: str | None = None
    folder_id: str | None = None
    import_error: str | None = None
    created_by: str | None = None
    edited_by: str | None = None
    # Lineage-derived (any version copilot-stamped); populated by the detail GET route only —
    # user saves re-stamp created_by/edited_by, so the current version alone is not durable.
    copilot_authored: bool = False

    @field_validator("run_with", mode="before")
    @classmethod
    def _normalize_run_with(cls, v: str | None) -> str:
        return normalize_run_with(v)

    @field_serializer("cdp_connect_headers")
    def _mask_cdp_connect_headers(self, headers: dict[str, str] | None) -> dict[str, str] | None:
        return mask_header_values(headers)

    created_at: datetime
    modified_at: datetime
    deleted_at: datetime | None = None

    def get_output_parameter(self, label: str) -> OutputParameter | None:
        for block in get_all_blocks(self.workflow_definition.blocks):
            if block.label == label:
                return block.output_parameter
        return None

    def get_parameter(self, key: str) -> PARAMETER_TYPE | None:
        for parameter in self.workflow_definition.parameters:
            if parameter.key == key:
                return parameter
        return None


class WorkflowRunStatus(StrEnum):
    created = "created"
    queued = "queued"
    running = "running"
    failed = "failed"
    terminated = "terminated"
    canceled = "canceled"
    timed_out = "timed_out"
    completed = "completed"
    paused = "paused"

    def is_final(self) -> bool:
        return self in [
            WorkflowRunStatus.failed,
            WorkflowRunStatus.terminated,
            WorkflowRunStatus.canceled,
            WorkflowRunStatus.timed_out,
            WorkflowRunStatus.completed,
        ]

    def is_final_excluding_canceled(self) -> bool:
        """Like :meth:`is_final` but excludes ``canceled``.

        For callers that can't distinguish a legitimate user/block cancel from
        a synthetic ``canceled`` written as a last-resort fallback — e.g. the
        copilot tool reading the row AFTER ``mark_workflow_run_as_canceled_if_not_final``
        has run. Callers that want to trust a legitimate ``canceled`` must read
        the row BEFORE invoking any cancel helper.
        """
        return self.is_final() and self is not WorkflowRunStatus.canceled


class WorkflowRun(BaseModel):
    workflow_run_id: str
    workflow_id: str
    workflow_permanent_id: str
    organization_id: str
    browser_session_id: str | None = None
    browser_profile_id: str | None = None
    browser_seed_source: BrowserSeedSource | None = None
    browser_sink_profile_id: str | None = None
    start_fresh_browser: bool | None = None
    reuse_browser_session: bool | None = None
    # Internal admission identity. It can contain routing inputs and must never enter API payloads.
    reuse_bound_key: str | None = Field(default=None, exclude=True)
    debug_session_id: str | None = None
    status: WorkflowRunStatus
    extra_http_headers: dict[str, str] | None = None
    cdp_connect_headers: dict[str, str] | None = None
    proxy_location: ProxyLocationInput = None
    webhook_callback_url: str | None = None
    webhook_failure_reason: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    failure_reason: str | None = None
    failure_category: list[dict[str, Any]] | None = None
    retried_from_workflow_run_id: str | None = None
    fallback_attempt: int | None = None
    parent_workflow_run_id: str | None = None
    workflow_title: str | None = None
    max_screenshot_scrolls: int | None = None
    max_elapsed_time_minutes: int | None = None
    browser_address: str | None = None
    run_with: str | None = None
    script_run: ScriptRunResponse | None = None
    job_id: str | None = None
    depends_on_workflow_run_id: str | None = None
    sequential_key: str | None = None
    sequential_credential_id: str | None = None
    ai_fallback: bool | None = None
    code_gen: bool | None = None
    trigger_type: WorkflowRunTriggerType | None = None
    workflow_schedule_id: str | None = None
    ignore_inherited_workflow_system_prompt: bool = False
    copilot_session_id: str | None = None
    credits_used: int = 0
    cached_credits_used: int = 0

    @field_validator("run_with", mode="before")
    @classmethod
    def _normalize_run_with(cls, v: str | None) -> str | None:
        """Normalize legacy values but preserve None (means 'inherit from workflow')."""
        if v is None:
            return None
        return normalize_run_with(v)

    @field_serializer("cdp_connect_headers")
    def _mask_cdp_connect_headers(self, headers: dict[str, str] | None) -> dict[str, str] | None:
        return mask_header_values(headers)

    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    modified_at: datetime

    @property
    def is_debug_session(self) -> bool:
        return self.debug_session_id is not None


def resolve_reuse_browser_session(*, run_override: bool | None, workflow_default: bool) -> bool:
    """Resolve browser-session reuse with the run override taking precedence."""
    return workflow_default if run_override is None else run_override


def should_acquire_reused_session(
    *,
    browser_session_id: str | None,
    start_fresh_browser: bool | None,
    run_override: bool | None,
    workflow_default: bool,
) -> bool:
    """Whether this run should acquire its workflow-bound browser session."""
    return (
        browser_session_id is None
        and not start_fresh_browser
        and resolve_reuse_browser_session(
            run_override=run_override,
            workflow_default=workflow_default,
        )
    )


def is_adaptive_caching_from_effective_state(
    *,
    workflow_run_with: str,
    run_run_with: str | None,
    code_version: int | None,
    adaptive_caching: bool,
) -> bool:
    """Compute adaptive caching from explicit workflow/run dispatch state.

    Uses code_version >= 2 as the primary check. Falls back to the legacy
    adaptive_caching bool for rows that haven't been backfilled yet
    (code_version is None).

    ``run_run_with`` is None when the run inherits from the workflow. This
    helper is shared by runtime and deploy-time cache-key resolution so the
    ``:v2`` suffix decision stays in one place.
    """
    run_with = normalize_run_with(run_run_with) if run_run_with is not None else normalize_run_with(workflow_run_with)
    if run_with == "agent":
        return False
    # run_with == "code": check code_version
    if run_with == "code":
        if code_version is not None:
            return code_version >= 2
        return adaptive_caching
    return False


def is_adaptive_caching(workflow: Workflow, workflow_run: WorkflowRun) -> bool:
    """Compute effective adaptive caching mode from run-level override or workflow setting."""
    return is_adaptive_caching_from_effective_state(
        workflow_run_with=workflow.run_with,
        run_run_with=workflow_run.run_with,
        code_version=workflow.code_version,
        adaptive_caching=workflow.adaptive_caching,
    )


class WorkflowRunParameter(BaseModel):
    workflow_run_id: str
    workflow_parameter_id: str
    value: bool | int | float | str | dict | list
    created_at: datetime


class WorkflowRunOutputParameter(BaseModel):
    workflow_run_id: str
    output_parameter_id: str
    value: dict[str, Any] | list | str | None
    created_at: datetime


class WorkflowRunResponseBase(BaseModel):
    workflow_id: str
    workflow_run_id: str

    @computed_field(description="Alias of `workflow_id` (the agent's `wpid_` permanent ID).")  # type: ignore[prop-decorator]
    @property
    def agent_id(self) -> str:
        return self.workflow_id

    @computed_field(description="Alias of `workflow_run_id`.")  # type: ignore[prop-decorator]
    @property
    def agent_run_id(self) -> str:
        return self.workflow_run_id

    status: WorkflowRunStatus
    failure_reason: str | None = None
    failure_category: list[dict[str, Any]] | None = None
    retried_from_workflow_run_id: str | None = None
    retried_by_workflow_run_id: str | None = None
    proxy_location: ProxyLocationInput = None
    webhook_callback_url: str | None = None
    webhook_failure_reason: str | None = None
    totp_verification_url: str | None = None
    totp_identifier: str | None = None
    extra_http_headers: dict[str, str] | None = None
    cdp_connect_headers: dict[str, str] | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    modified_at: datetime
    parameters: dict[str, Any]
    screenshot_urls: list[str] | None = None
    recording_url: str | None = None
    recording_urls: list[str] | None = None
    recording_archived: bool = False
    downloaded_files: list[FileInfo] | None = None
    downloaded_file_urls: list[str] | None = None
    outputs: dict[str, Any] | None = None
    total_steps: int | None = None
    total_cost: float | None = Field(
        default=None,
        description=(
            "Estimated workflow-run cost as the list-price value of credits consumed "
            "(credits x list credit rate), not the amount invoiced; enterprise contract "
            "pricing and legacy billing may differ. Null when cost is unavailable."
        ),
    )
    credits_used: int = 0
    cached_credits_used: int = 0
    task_v2: TaskV2 | None = None
    workflow_title: str | None = None
    browser_session_id: str | None = None
    browser_profile_id: str | None = None
    browser_seed_source: BrowserSeedSource | None = None
    browser_sink_profile_id: str | None = None
    max_screenshot_scrolls: int | None = None
    browser_address: str | None = None
    run_with: str = "agent"
    script_run: ScriptRunResponse | None = None
    script_id: str | None = None
    errors: list[dict[str, Any]] | None = None

    @field_validator("run_with", mode="before")
    @classmethod
    def _normalize_run_with(cls, v: str | None) -> str:
        return normalize_run_with(v)

    @field_serializer("cdp_connect_headers")
    def _mask_cdp_connect_headers(self, headers: dict[str, str] | None) -> dict[str, str] | None:
        return mask_header_values(headers)


class WorkflowRunWithWorkflowResponse(WorkflowRunResponseBase):
    workflow: Workflow
