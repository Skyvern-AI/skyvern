"""Injectable workflow copilot configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from skyvern.config import settings
from skyvern.forge.sdk.copilot.output_extraction_plan import ShapeExpectation


class BlockAuthoringPolicy(StrEnum):
    STANDARD = "standard"
    CODE_ONLY_BROWSER = "code_only_browser"
    TASK_V3_PURE = "task_v3_pure"


def normalize_block_authoring_policy(value: object) -> BlockAuthoringPolicy:
    if isinstance(value, BlockAuthoringPolicy):
        return value
    if isinstance(value, str):
        try:
            return BlockAuthoringPolicy(value)
        except ValueError:
            return BlockAuthoringPolicy.STANDARD
    return BlockAuthoringPolicy.STANDARD


def block_authoring_policy_from_code_only_mode(enabled: bool) -> BlockAuthoringPolicy:
    return BlockAuthoringPolicy.CODE_ONLY_BROWSER if enabled else BlockAuthoringPolicy.STANDARD


def block_authoring_policy_for_request(code_block_mode: bool | None) -> BlockAuthoringPolicy:
    if code_block_mode is True:
        return BlockAuthoringPolicy.CODE_ONLY_BROWSER
    return BlockAuthoringPolicy.TASK_V3_PURE


def download_scout_act_required_for_policy(block_authoring_policy: BlockAuthoringPolicy | str | None) -> bool:
    return normalize_block_authoring_policy(block_authoring_policy) == BlockAuthoringPolicy.CODE_ONLY_BROWSER


DEFAULT_PROMPT_TEMPLATE = "workflow-copilot-agent.j2"
DEFAULT_MAX_TURNS = 200
DEFAULT_TOKEN_BUDGET = 90_000

SCREENSHOT_DROPPED_NUDGE = (
    "Your previous screenshot was dropped from context to recover from a token-budget overflow. "
    "Do NOT reason about the page from memory. Re-take the screenshot "
    "(get_browser_screenshot) or call evaluate before deciding your next step."
)

DEFAULT_ENFORCEMENT_NUDGES: dict[str, str] = {
    "screenshot_dropped": SCREENSHOT_DROPPED_NUDGE,
}


def _default_enforcement_nudges() -> dict[str, str]:
    return dict(DEFAULT_ENFORCEMENT_NUDGES)


def _default_fallback_llm_key() -> str | None:
    return settings.SECONDARY_LLM_KEY


def _default_credential_pause_enabled() -> bool:
    return settings.WORKFLOW_COPILOT_CREDENTIAL_PAUSE_ENABLED


def _default_credential_pause_timeout_seconds() -> int:
    return settings.WORKFLOW_COPILOT_CREDENTIAL_PAUSE_TIMEOUT_SECONDS


def _default_token_budget() -> int:
    qa_token_budget = settings.WORKFLOW_COPILOT_QA_TOKEN_BUDGET
    if not settings.is_cloud_environment() and qa_token_budget is not None:
        return qa_token_budget
    return DEFAULT_TOKEN_BUDGET


@dataclass(slots=True)
class CopilotConfig:
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE
    max_turns: int = DEFAULT_MAX_TURNS
    token_budget: int = field(default_factory=_default_token_budget)
    security_rules: str = ""
    enforcement_nudges: dict[str, str] = field(default_factory=_default_enforcement_nudges)
    fallback_llm_key: str | None = field(default_factory=_default_fallback_llm_key)
    block_authoring_policy: BlockAuthoringPolicy = BlockAuthoringPolicy.STANDARD
    code_block_available: bool = False
    effective_code_block_mode: bool = False
    requested_output_path_aliases: dict[str, str] = field(default_factory=dict)
    requested_output_shape_expectations: dict[str, ShapeExpectation] = field(default_factory=dict)
    credential_pause_enabled: bool = field(default_factory=_default_credential_pause_enabled)
    credential_pause_timeout_seconds: int = field(default_factory=_default_credential_pause_timeout_seconds)

    def nudge(self, key: str) -> str:
        return self.enforcement_nudges.get(key, DEFAULT_ENFORCEMENT_NUDGES[key])
