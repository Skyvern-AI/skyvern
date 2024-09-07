from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_serializer


class ArtifactType(StrEnum):
    RECORDING = "recording"

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
    VISIBLE_ELEMENTS_ID_CSS_MAP = "visible_elements_id_css_map"
    VISIBLE_ELEMENTS_ID_FRAME_MAP = "visible_elements_id_frame_map"
    VISIBLE_ELEMENTS_TREE = "visible_elements_tree"
    VISIBLE_ELEMENTS_TREE_TRIMMED = "visible_elements_tree_trimmed"
    VISIBLE_ELEMENTS_TREE_IN_PROMPT = "visible_elements_tree_in_prompt"

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

    artifact_id: str = Field(
        ...,
        description="The ID of the task artifact.",
        examples=["6bb1801a-fd80-45e8-899a-4dd723cc602e"],
    )
    task_id: str = Field(
        ...,
        description="The ID of the task this artifact belongs to.",
        examples=["50da533e-3904-4401-8a07-c49adf88b5eb"],
    )
    step_id: str = Field(
        ...,
        description="The ID of the task step this artifact belongs to.",
        examples=["6bb1801a-fd80-45e8-899a-4dd723cc602e"],
    )
    artifact_type: ArtifactType = Field(
        ...,
        description="The type of the artifact.",
        examples=["screenshot"],
    )
    uri: str = Field(
        ...,
        description="The URI of the artifact.",
        examples=["/Users/skyvern/hello/world.png"],
    )
    signed_url: str | None = None
    organization_id: str | None = None
