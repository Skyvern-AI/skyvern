from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_serializer


class ArtifactType(StrEnum):
    RECORDING = "recording"
    BROWSER_CONSOLE_LOG = "browser_console_log"

    SKYVERN_LOG = "skyvern_log"
    SKYVERN_LOG_RAW = "skyvern_log_raw"

    # DEPRECATED. pls use SCREENSHOT_LLM, SCREENSHOT_ACTION or SCREENSHOT_FINAL
    SCREENSHOT = "screenshot"

    # USE THESE for screenshots
    SCREENSHOT_LLM = "screenshot_llm"
    SCREENSHOT_ACTION = "screenshot_action"
    SCREENSHOT_FINAL = "screenshot_final"

    LLM_PROMPT = "llm_prompt"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    LLM_RESPONSE_PARSED = "llm_response_parsed"
    LLM_RESPONSE_RENDERED = "llm_response_rendered"
    VISIBLE_ELEMENTS_ID_CSS_MAP = "visible_elements_id_css_map"
    VISIBLE_ELEMENTS_ID_FRAME_MAP = "visible_elements_id_frame_map"
    VISIBLE_ELEMENTS_TREE = "visible_elements_tree"
    VISIBLE_ELEMENTS_TREE_TRIMMED = "visible_elements_tree_trimmed"
    VISIBLE_ELEMENTS_TREE_IN_PROMPT = "visible_elements_tree_in_prompt"

    HASHED_HREF_MAP = "hashed_href_map"

    # DEPRECATED. pls use VISIBLE_ELEMENTS_ID_CSS_MAP
    VISIBLE_ELEMENTS_ID_XPATH_MAP = "visible_elements_id_xpath_map"

    # DEPRECATED. pls use HTML_SCRAPE or HTML_ACTION
    HTML = "html"

    # USE THESE for htmls
    HTML_SCRAPE = "html_scrape"
    HTML_ACTION = "html_action"

    # Debugging
    TRACE = "trace"
    HAR = "har"


class Artifact(BaseModel):
    created_at: datetime = Field(
        ...,
        description="The creation datetime of the task.",
        examples=["2023-01-01T00:00:00Z"],
    )
    modified_at: datetime = Field(
        ...,
        description="The modification datetime of the task.",
        examples=["2023-01-01T00:00:00Z"],
    )

    @field_serializer("created_at", "modified_at", when_used="json")
    def serialize_datetime_to_isoformat(self, value: datetime) -> str:
        return value.isoformat()

    artifact_id: str
    artifact_type: ArtifactType
    uri: str
    task_id: str | None = None
    step_id: str | None = None
    workflow_run_id: str | None = None
    workflow_run_block_id: str | None = None
    observer_cruise_id: str | None = None
    observer_thought_id: str | None = None
    ai_suggestion_id: str | None = None
    signed_url: str | None = None
    organization_id: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class LogEntityType(StrEnum):
    STEP = "step"
    TASK = "task"
    WORKFLOW_RUN = "workflow_run"
    WORKFLOW_RUN_BLOCK = "workflow_run_block"
    TASK_V2 = "task_v2"
