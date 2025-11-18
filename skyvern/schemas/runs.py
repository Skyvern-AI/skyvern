from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Union
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from skyvern.forge.sdk.schemas.files import FileInfo
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
    TASK_ENGINE_DOC_STRING,
    TASK_PROMPT_DOC_STRING,
    TASK_URL_DOC_STRING,
    TOTP_IDENTIFIER_DOC_STRING,
    TOTP_URL_DOC_STRING,
    WEBHOOK_URL_DOC_STRING,
)
from skyvern.schemas.proxy_locations import (
    build_iso_by_residential_location,
    build_residential_locations_by_iso,
    build_tzinfo_by_residential_location,
)
from skyvern.utils.url_validators import validate_url


class ProxyLocation(StrEnum):
    RESIDENTIAL = "RESIDENTIAL"
    US_CA = "US-CA"
    US_NY = "US-NY"
    US_TX = "US-TX"
    US_FL = "US-FL"
    US_WA = "US-WA"
    RESIDENTIAL_ISP = "RESIDENTIAL_ISP"
    NONE = "NONE"
    # JoinMassive residential locations
    RESIDENTIAL_AD = "RESIDENTIAL_AD"
    RESIDENTIAL_AE = "RESIDENTIAL_AE"
    RESIDENTIAL_AF = "RESIDENTIAL_AF"
    RESIDENTIAL_AG = "RESIDENTIAL_AG"
    RESIDENTIAL_AI = "RESIDENTIAL_AI"
    RESIDENTIAL_AL = "RESIDENTIAL_AL"
    RESIDENTIAL_AM = "RESIDENTIAL_AM"
    RESIDENTIAL_AO = "RESIDENTIAL_AO"
    RESIDENTIAL_AR = "RESIDENTIAL_AR"
    RESIDENTIAL_AS = "RESIDENTIAL_AS"
    RESIDENTIAL_AT = "RESIDENTIAL_AT"
    RESIDENTIAL_AU = "RESIDENTIAL_AU"
    RESIDENTIAL_AW = "RESIDENTIAL_AW"
    RESIDENTIAL_AX = "RESIDENTIAL_AX"
    RESIDENTIAL_AZ = "RESIDENTIAL_AZ"
    RESIDENTIAL_BA = "RESIDENTIAL_BA"
    RESIDENTIAL_BB = "RESIDENTIAL_BB"
    RESIDENTIAL_BD = "RESIDENTIAL_BD"
    RESIDENTIAL_BE = "RESIDENTIAL_BE"
    RESIDENTIAL_BF = "RESIDENTIAL_BF"
    RESIDENTIAL_BG = "RESIDENTIAL_BG"
    RESIDENTIAL_BH = "RESIDENTIAL_BH"
    RESIDENTIAL_BI = "RESIDENTIAL_BI"
    RESIDENTIAL_BJ = "RESIDENTIAL_BJ"
    RESIDENTIAL_BM = "RESIDENTIAL_BM"
    RESIDENTIAL_BN = "RESIDENTIAL_BN"
    RESIDENTIAL_BO = "RESIDENTIAL_BO"
    RESIDENTIAL_BQ = "RESIDENTIAL_BQ"
    RESIDENTIAL_BR = "RESIDENTIAL_BR"
    RESIDENTIAL_BS = "RESIDENTIAL_BS"
    RESIDENTIAL_BT = "RESIDENTIAL_BT"
    RESIDENTIAL_BW = "RESIDENTIAL_BW"
    RESIDENTIAL_BY = "RESIDENTIAL_BY"
    RESIDENTIAL_BZ = "RESIDENTIAL_BZ"
    RESIDENTIAL_CA = "RESIDENTIAL_CA"
    RESIDENTIAL_CD = "RESIDENTIAL_CD"
    RESIDENTIAL_CF = "RESIDENTIAL_CF"
    RESIDENTIAL_CG = "RESIDENTIAL_CG"
    RESIDENTIAL_CH = "RESIDENTIAL_CH"
    RESIDENTIAL_CI = "RESIDENTIAL_CI"
    RESIDENTIAL_CK = "RESIDENTIAL_CK"
    RESIDENTIAL_CL = "RESIDENTIAL_CL"
    RESIDENTIAL_CM = "RESIDENTIAL_CM"
    RESIDENTIAL_CN = "RESIDENTIAL_CN"
    RESIDENTIAL_CO = "RESIDENTIAL_CO"
    RESIDENTIAL_CR = "RESIDENTIAL_CR"
    RESIDENTIAL_CU = "RESIDENTIAL_CU"
    RESIDENTIAL_CV = "RESIDENTIAL_CV"
    RESIDENTIAL_CW = "RESIDENTIAL_CW"
    RESIDENTIAL_CY = "RESIDENTIAL_CY"
    RESIDENTIAL_CZ = "RESIDENTIAL_CZ"
    RESIDENTIAL_DE = "RESIDENTIAL_DE"
    RESIDENTIAL_DJ = "RESIDENTIAL_DJ"
    RESIDENTIAL_DK = "RESIDENTIAL_DK"
    RESIDENTIAL_DM = "RESIDENTIAL_DM"
    RESIDENTIAL_DO = "RESIDENTIAL_DO"
    RESIDENTIAL_DZ = "RESIDENTIAL_DZ"
    RESIDENTIAL_EC = "RESIDENTIAL_EC"
    RESIDENTIAL_EE = "RESIDENTIAL_EE"
    RESIDENTIAL_EG = "RESIDENTIAL_EG"
    RESIDENTIAL_ES = "RESIDENTIAL_ES"
    RESIDENTIAL_ET = "RESIDENTIAL_ET"
    RESIDENTIAL_FI = "RESIDENTIAL_FI"
    RESIDENTIAL_FJ = "RESIDENTIAL_FJ"
    RESIDENTIAL_FK = "RESIDENTIAL_FK"
    RESIDENTIAL_FM = "RESIDENTIAL_FM"
    RESIDENTIAL_FO = "RESIDENTIAL_FO"
    RESIDENTIAL_FR = "RESIDENTIAL_FR"
    RESIDENTIAL_GA = "RESIDENTIAL_GA"
    RESIDENTIAL_GB = "RESIDENTIAL_GB"
    RESIDENTIAL_GD = "RESIDENTIAL_GD"
    RESIDENTIAL_GE = "RESIDENTIAL_GE"
    RESIDENTIAL_GF = "RESIDENTIAL_GF"
    RESIDENTIAL_GG = "RESIDENTIAL_GG"
    RESIDENTIAL_GH = "RESIDENTIAL_GH"
    RESIDENTIAL_GI = "RESIDENTIAL_GI"
    RESIDENTIAL_GL = "RESIDENTIAL_GL"
    RESIDENTIAL_GM = "RESIDENTIAL_GM"
    RESIDENTIAL_GN = "RESIDENTIAL_GN"
    RESIDENTIAL_GP = "RESIDENTIAL_GP"
    RESIDENTIAL_GQ = "RESIDENTIAL_GQ"
    RESIDENTIAL_GR = "RESIDENTIAL_GR"
    RESIDENTIAL_GT = "RESIDENTIAL_GT"
    RESIDENTIAL_GU = "RESIDENTIAL_GU"
    RESIDENTIAL_GW = "RESIDENTIAL_GW"
    RESIDENTIAL_GY = "RESIDENTIAL_GY"
    RESIDENTIAL_HK = "RESIDENTIAL_HK"
    RESIDENTIAL_HN = "RESIDENTIAL_HN"
    RESIDENTIAL_HR = "RESIDENTIAL_HR"
    RESIDENTIAL_HT = "RESIDENTIAL_HT"
    RESIDENTIAL_HU = "RESIDENTIAL_HU"
    RESIDENTIAL_ID = "RESIDENTIAL_ID"
    RESIDENTIAL_IE = "RESIDENTIAL_IE"
    RESIDENTIAL_IL = "RESIDENTIAL_IL"
    RESIDENTIAL_IM = "RESIDENTIAL_IM"
    RESIDENTIAL_IN = "RESIDENTIAL_IN"
    RESIDENTIAL_IO = "RESIDENTIAL_IO"
    RESIDENTIAL_IQ = "RESIDENTIAL_IQ"
    RESIDENTIAL_IR = "RESIDENTIAL_IR"
    RESIDENTIAL_IS = "RESIDENTIAL_IS"
    RESIDENTIAL_IT = "RESIDENTIAL_IT"
    RESIDENTIAL_JE = "RESIDENTIAL_JE"
    RESIDENTIAL_JM = "RESIDENTIAL_JM"
    RESIDENTIAL_JO = "RESIDENTIAL_JO"
    RESIDENTIAL_JP = "RESIDENTIAL_JP"
    RESIDENTIAL_KE = "RESIDENTIAL_KE"
    RESIDENTIAL_KG = "RESIDENTIAL_KG"
    RESIDENTIAL_KH = "RESIDENTIAL_KH"
    RESIDENTIAL_KI = "RESIDENTIAL_KI"
    RESIDENTIAL_KM = "RESIDENTIAL_KM"
    RESIDENTIAL_KN = "RESIDENTIAL_KN"
    RESIDENTIAL_KR = "RESIDENTIAL_KR"
    RESIDENTIAL_KW = "RESIDENTIAL_KW"
    RESIDENTIAL_KY = "RESIDENTIAL_KY"
    RESIDENTIAL_KZ = "RESIDENTIAL_KZ"
    RESIDENTIAL_LA = "RESIDENTIAL_LA"
    RESIDENTIAL_LB = "RESIDENTIAL_LB"
    RESIDENTIAL_LC = "RESIDENTIAL_LC"
    RESIDENTIAL_LI = "RESIDENTIAL_LI"
    RESIDENTIAL_LK = "RESIDENTIAL_LK"
    RESIDENTIAL_LR = "RESIDENTIAL_LR"
    RESIDENTIAL_LS = "RESIDENTIAL_LS"
    RESIDENTIAL_LT = "RESIDENTIAL_LT"
    RESIDENTIAL_LU = "RESIDENTIAL_LU"
    RESIDENTIAL_LV = "RESIDENTIAL_LV"
    RESIDENTIAL_LY = "RESIDENTIAL_LY"
    RESIDENTIAL_MA = "RESIDENTIAL_MA"
    RESIDENTIAL_MC = "RESIDENTIAL_MC"
    RESIDENTIAL_MD = "RESIDENTIAL_MD"
    RESIDENTIAL_ME = "RESIDENTIAL_ME"
    RESIDENTIAL_MF = "RESIDENTIAL_MF"
    RESIDENTIAL_MG = "RESIDENTIAL_MG"
    RESIDENTIAL_MK = "RESIDENTIAL_MK"
    RESIDENTIAL_ML = "RESIDENTIAL_ML"
    RESIDENTIAL_MM = "RESIDENTIAL_MM"
    RESIDENTIAL_MN = "RESIDENTIAL_MN"
    RESIDENTIAL_MO = "RESIDENTIAL_MO"
    RESIDENTIAL_MP = "RESIDENTIAL_MP"
    RESIDENTIAL_MQ = "RESIDENTIAL_MQ"
    RESIDENTIAL_MR = "RESIDENTIAL_MR"
    RESIDENTIAL_MS = "RESIDENTIAL_MS"
    RESIDENTIAL_MT = "RESIDENTIAL_MT"
    RESIDENTIAL_MU = "RESIDENTIAL_MU"
    RESIDENTIAL_MV = "RESIDENTIAL_MV"
    RESIDENTIAL_MW = "RESIDENTIAL_MW"
    RESIDENTIAL_MX = "RESIDENTIAL_MX"
    RESIDENTIAL_MY = "RESIDENTIAL_MY"
    RESIDENTIAL_MZ = "RESIDENTIAL_MZ"
    RESIDENTIAL_NA = "RESIDENTIAL_NA"
    RESIDENTIAL_NC = "RESIDENTIAL_NC"
    RESIDENTIAL_NE = "RESIDENTIAL_NE"
    RESIDENTIAL_NG = "RESIDENTIAL_NG"
    RESIDENTIAL_NI = "RESIDENTIAL_NI"
    RESIDENTIAL_NL = "RESIDENTIAL_NL"
    RESIDENTIAL_NO = "RESIDENTIAL_NO"
    RESIDENTIAL_NP = "RESIDENTIAL_NP"
    RESIDENTIAL_NZ = "RESIDENTIAL_NZ"
    RESIDENTIAL_OM = "RESIDENTIAL_OM"
    RESIDENTIAL_PA = "RESIDENTIAL_PA"
    RESIDENTIAL_PE = "RESIDENTIAL_PE"
    RESIDENTIAL_PF = "RESIDENTIAL_PF"
    RESIDENTIAL_PG = "RESIDENTIAL_PG"
    RESIDENTIAL_PH = "RESIDENTIAL_PH"
    RESIDENTIAL_PK = "RESIDENTIAL_PK"
    RESIDENTIAL_PL = "RESIDENTIAL_PL"
    RESIDENTIAL_PM = "RESIDENTIAL_PM"
    RESIDENTIAL_PR = "RESIDENTIAL_PR"
    RESIDENTIAL_PS = "RESIDENTIAL_PS"
    RESIDENTIAL_PT = "RESIDENTIAL_PT"
    RESIDENTIAL_PW = "RESIDENTIAL_PW"
    RESIDENTIAL_PY = "RESIDENTIAL_PY"
    RESIDENTIAL_QA = "RESIDENTIAL_QA"
    RESIDENTIAL_RE = "RESIDENTIAL_RE"
    RESIDENTIAL_RO = "RESIDENTIAL_RO"
    RESIDENTIAL_RS = "RESIDENTIAL_RS"
    RESIDENTIAL_RU = "RESIDENTIAL_RU"
    RESIDENTIAL_RW = "RESIDENTIAL_RW"
    RESIDENTIAL_SA = "RESIDENTIAL_SA"
    RESIDENTIAL_SB = "RESIDENTIAL_SB"
    RESIDENTIAL_SC = "RESIDENTIAL_SC"
    RESIDENTIAL_SD = "RESIDENTIAL_SD"
    RESIDENTIAL_SE = "RESIDENTIAL_SE"
    RESIDENTIAL_SG = "RESIDENTIAL_SG"
    RESIDENTIAL_SI = "RESIDENTIAL_SI"
    RESIDENTIAL_SK = "RESIDENTIAL_SK"
    RESIDENTIAL_SL = "RESIDENTIAL_SL"
    RESIDENTIAL_SM = "RESIDENTIAL_SM"
    RESIDENTIAL_SN = "RESIDENTIAL_SN"
    RESIDENTIAL_SO = "RESIDENTIAL_SO"
    RESIDENTIAL_SR = "RESIDENTIAL_SR"
    RESIDENTIAL_SS = "RESIDENTIAL_SS"
    RESIDENTIAL_ST = "RESIDENTIAL_ST"
    RESIDENTIAL_SV = "RESIDENTIAL_SV"
    RESIDENTIAL_SX = "RESIDENTIAL_SX"
    RESIDENTIAL_SY = "RESIDENTIAL_SY"
    RESIDENTIAL_SZ = "RESIDENTIAL_SZ"
    RESIDENTIAL_TC = "RESIDENTIAL_TC"
    RESIDENTIAL_TD = "RESIDENTIAL_TD"
    RESIDENTIAL_TG = "RESIDENTIAL_TG"
    RESIDENTIAL_TH = "RESIDENTIAL_TH"
    RESIDENTIAL_TJ = "RESIDENTIAL_TJ"
    RESIDENTIAL_TL = "RESIDENTIAL_TL"
    RESIDENTIAL_TM = "RESIDENTIAL_TM"
    RESIDENTIAL_TN = "RESIDENTIAL_TN"
    RESIDENTIAL_TO = "RESIDENTIAL_TO"
    RESIDENTIAL_TR = "RESIDENTIAL_TR"
    RESIDENTIAL_TT = "RESIDENTIAL_TT"
    RESIDENTIAL_TW = "RESIDENTIAL_TW"
    RESIDENTIAL_TZ = "RESIDENTIAL_TZ"
    RESIDENTIAL_UA = "RESIDENTIAL_UA"
    RESIDENTIAL_UG = "RESIDENTIAL_UG"
    RESIDENTIAL_UY = "RESIDENTIAL_UY"
    RESIDENTIAL_UZ = "RESIDENTIAL_UZ"
    RESIDENTIAL_VA = "RESIDENTIAL_VA"
    RESIDENTIAL_VC = "RESIDENTIAL_VC"
    RESIDENTIAL_VE = "RESIDENTIAL_VE"
    RESIDENTIAL_VG = "RESIDENTIAL_VG"
    RESIDENTIAL_VI = "RESIDENTIAL_VI"
    RESIDENTIAL_VN = "RESIDENTIAL_VN"
    RESIDENTIAL_VU = "RESIDENTIAL_VU"
    RESIDENTIAL_WS = "RESIDENTIAL_WS"
    RESIDENTIAL_XK = "RESIDENTIAL_XK"
    RESIDENTIAL_YE = "RESIDENTIAL_YE"
    RESIDENTIAL_YT = "RESIDENTIAL_YT"
    RESIDENTIAL_ZA = "RESIDENTIAL_ZA"
    RESIDENTIAL_ZM = "RESIDENTIAL_ZM"
    RESIDENTIAL_ZW = "RESIDENTIAL_ZW"

    @staticmethod
    def get_zone(proxy_location: ProxyLocation) -> str:
        zone_mapping = {
            ProxyLocation.US_CA: "california",
            ProxyLocation.US_NY: "newyork",
            ProxyLocation.US_TX: "texas",
            ProxyLocation.US_FL: "florida",
            ProxyLocation.US_WA: "washington",
            ProxyLocation.RESIDENTIAL: "residential_long-country-us",
        }
        if proxy_location in zone_mapping:
            return zone_mapping[proxy_location]
        raise ValueError(f"No zone mapping for proxy location: {proxy_location}")

    @classmethod
    def residential_country_locations(cls) -> set[ProxyLocation]:
        return set(_RESIDENTIAL_LOCATIONS_BY_ISO.values())

    @staticmethod
    def get_proxy_count(proxy_location: ProxyLocation) -> int:
        iso_code = _ISO_BY_RESIDENTIAL_LOCATION.get(proxy_location)
        if iso_code == "US":
            return 10000
        if iso_code is not None:
            return 2000
        return 10000

    @staticmethod
    def get_country_code(proxy_location: ProxyLocation) -> str:
        return _ISO_BY_RESIDENTIAL_LOCATION.get(proxy_location, "US")


_RESIDENTIAL_LOCATIONS_BY_ISO = build_residential_locations_by_iso(ProxyLocation)
_ISO_BY_RESIDENTIAL_LOCATION = build_iso_by_residential_location(_RESIDENTIAL_LOCATIONS_BY_ISO)
_TZINFO_BY_RESIDENTIAL_LOCATION = build_tzinfo_by_residential_location(_RESIDENTIAL_LOCATIONS_BY_ISO)


def get_tzinfo_from_proxy(proxy_location: ProxyLocation) -> ZoneInfo | None:
    if proxy_location == ProxyLocation.NONE:
        return None

    if proxy_location == ProxyLocation.US_CA:
        return ZoneInfo("America/Los_Angeles")

    if proxy_location == ProxyLocation.US_NY:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.US_TX:
        return ZoneInfo("America/Chicago")

    if proxy_location == ProxyLocation.US_FL:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.US_WA:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.RESIDENTIAL:
        return ZoneInfo("America/New_York")

    if proxy_location == ProxyLocation.RESIDENTIAL_ISP:
        return ZoneInfo("America/New_York")

    tzinfo = _TZINFO_BY_RESIDENTIAL_LOCATION.get(proxy_location)
    if tzinfo is not None:
        return tzinfo

    return None


class RunType(StrEnum):
    task_v1 = "task_v1"
    task_v2 = "task_v2"
    workflow_run = "workflow_run"
    openai_cua = "openai_cua"
    anthropic_cua = "anthropic_cua"
    ui_tars = "ui_tars"


class RunEngine(StrEnum):
    skyvern_v1 = "skyvern-1.0"
    skyvern_v2 = "skyvern-2.0"
    openai_cua = "openai-cua"
    anthropic_cua = "anthropic-cua"
    ui_tars = "ui-tars"


CUA_ENGINES = [RunEngine.openai_cua, RunEngine.anthropic_cua, RunEngine.ui_tars]
CUA_RUN_TYPES = [RunType.openai_cua, RunType.anthropic_cua, RunType.ui_tars]


class RunStatus(StrEnum):
    created = "created"
    queued = "queued"
    running = "running"
    timed_out = "timed_out"
    failed = "failed"
    terminated = "terminated"
    completed = "completed"
    canceled = "canceled"

    def is_final(self) -> bool:
        return self in [self.failed, self.terminated, self.canceled, self.timed_out, self.completed]


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
        default=RunEngine.skyvern_v2,
        description=TASK_ENGINE_DOC_STRING,
    )
    title: str | None = Field(
        default=None, description="The title for the task", examples=["The title of my first skyvern task"]
    )
    proxy_location: ProxyLocation | None = Field(
        default=ProxyLocation.RESIDENTIAL,
        description=PROXY_LOCATION_DOC_STRING,
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
    model: dict[str, Any] | None = Field(
        default=None,
        description=MODEL_CONFIG,
        examples=None,
    )
    extra_http_headers: dict[str, str] | None = Field(
        default=None,
        description="The extra HTTP headers for the requests in browser.",
    )
    publish_workflow: bool = Field(
        default=False,
        description="Whether to publish this task as a reusable workflow. Only available for skyvern-2.0.",
    )
    include_action_history_in_verification: bool | None = Field(
        default=False, description="Whether to include action history when verifying that the task is complete"
    )
    max_screenshot_scrolls: int | None = Field(
        default=None,
        description="The maximum number of scrolls for the post action screenshot. When it's None or 0, it takes the current viewpoint screenshot.",
    )
    browser_address: str | None = Field(
        default=None,
        description="The CDP address for the task.",
        examples=["http://127.0.0.1:9222", "ws://127.0.0.1:9222/devtools/browser/1234567890"],
    )

    @field_validator("url", "webhook_url", "totp_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        """
        Validates that URLs provided to Skyvern are properly formatted.

        Args:
            url: The URL for Skyvern to validate

        Returns:
            The validated URL or None if no URL was provided
        """
        if url is None:
            return None

        return validate_url(url)


class WorkflowRunRequest(BaseModel):
    workflow_id: str = Field(
        description="ID of the workflow to run. Workflow ID starts with `wpid_`.", examples=["wpid_123"]
    )
    parameters: dict[str, Any] | None = Field(default=None, description="Parameters to pass to the workflow")
    title: str | None = Field(default=None, description="The title for this workflow run")
    proxy_location: ProxyLocation | None = Field(
        default=ProxyLocation.RESIDENTIAL,
        description=PROXY_LOCATION_DOC_STRING,
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
    browser_profile_id: str | None = Field(
        default=None,
        description="ID of a browser profile to reuse for this workflow run",
    )
    max_screenshot_scrolls: int | None = Field(
        default=None,
        description="The maximum number of scrolls for the post action screenshot. When it's None or 0, it takes the current viewpoint screenshot.",
    )
    extra_http_headers: dict[str, str] | None = Field(
        default=None,
        description="The extra HTTP headers for the requests in browser.",
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
        description="Whether to run the workflow with agent or code.",
    )

    @field_validator("webhook_url", "totp_url")
    @classmethod
    def validate_urls(cls, url: str | None) -> str | None:
        if url is None:
            return None
        return validate_url(url)

    @model_validator(mode="after")
    def validate_browser_reference(cls, values: WorkflowRunRequest) -> WorkflowRunRequest:
        if values.browser_session_id and values.browser_profile_id:
            raise ValueError("Cannot specify both browser_session_id and browser_profile_id")
        return values


class BlockRunRequest(WorkflowRunRequest):
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


class ScriptRunResponse(BaseModel):
    ai_fallback_triggered: bool = False


class BaseRunResponse(BaseModel):
    run_id: str = Field(
        description="Unique identifier for this run. Run ID starts with `tsk_` for task runs and `wr_` for workflow runs.",
        examples=["tsk_123", "tsk_v2_123", "wr_123"],
    )
    status: RunStatus = Field(
        description="Current status of the run",
        examples=["created", "queued", "running", "timed_out", "failed", "terminated", "completed", "canceled"],
    )
    output: dict | list | str | None = Field(
        default=None,
        description="Output data from the run, if any. Format/schema depends on the data extracted by the run.",
    )
    downloaded_files: list[FileInfo] | None = Field(default=None, description="List of files downloaded during the run")
    recording_url: str | None = Field(default=None, description="URL to the recording of the run")
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


class TaskRunResponse(BaseRunResponse):
    run_type: Literal[RunType.task_v1, RunType.task_v2, RunType.openai_cua, RunType.anthropic_cua, RunType.ui_tars] = (
        Field(description="Types of a task run - task_v1, task_v2, openai_cua, anthropic_cua, ui_tars")
    )
    run_request: TaskRunRequest | None = Field(
        default=None, description="The original request parameters used to start this task run"
    )


class WorkflowRunResponse(BaseRunResponse):
    run_type: Literal[RunType.workflow_run] = Field(description="Type of run - always workflow_run for workflow runs")
    run_with: str | None = Field(
        default=None,
        description="Whether the workflow run was executed with agent or code",
        examples=["agent", "code"],
    )
    ai_fallback: bool | None = Field(
        default=None,
        description="Whether to fallback to AI if code run fails.",
    )
    run_request: WorkflowRunRequest | None = Field(
        default=None, description="The original request parameters used to start this workflow run"
    )


RunResponse = Annotated[Union[TaskRunResponse, WorkflowRunResponse], Field(discriminator="run_type")]


class BlockRunResponse(WorkflowRunResponse):
    block_labels: list[str] = Field(description="A whitelist of block labels; only these blocks will execute")
