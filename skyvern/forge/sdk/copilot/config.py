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


@dataclass(frozen=True, slots=True)
class AuthoringCapability:
    """Which block families this turn may author. The one fact every authoring surface keys on."""

    code_blocks: bool
    agent_blocks: bool


AGENT_BLOCKS_ONLY = AuthoringCapability(code_blocks=False, agent_blocks=True)
CODE_BLOCKS_ONLY = AuthoringCapability(code_blocks=True, agent_blocks=False)
ALL_BLOCK_FAMILIES = AuthoringCapability(code_blocks=True, agent_blocks=True)

_POLICY_CAPABILITIES: dict[BlockAuthoringPolicy, AuthoringCapability] = {
    BlockAuthoringPolicy.STANDARD: ALL_BLOCK_FAMILIES,
    BlockAuthoringPolicy.CODE_ONLY_BROWSER: CODE_BLOCKS_ONLY,
    BlockAuthoringPolicy.TASK_V3_PURE: AGENT_BLOCKS_ONLY,
}


def authoring_capability_for_request(code_block_mode: bool | None, has_code_block_access: bool) -> AuthoringCapability:
    if not has_code_block_access or code_block_mode is False:
        return AGENT_BLOCKS_ONLY
    return AuthoringCapability(code_blocks=True, agent_blocks=code_block_mode is None)


def authoring_capability_from_policy(policy: BlockAuthoringPolicy | str | None) -> AuthoringCapability:
    """A carrier that states no policy, or one nobody recognises, authors no code: code authoring is
    granted, never assumed. Deliberately stricter than :func:`normalize_block_authoring_policy`,
    which answers a different question and resolves an unknown spelling to ``STANDARD``."""
    if isinstance(policy, BlockAuthoringPolicy):
        return _POLICY_CAPABILITIES[policy]
    if isinstance(policy, str):
        try:
            return _POLICY_CAPABILITIES[BlockAuthoringPolicy(policy)]
        except ValueError:
            return AGENT_BLOCKS_ONLY
    return AGENT_BLOCKS_ONLY


def block_authoring_policy_from_capability(capability: AuthoringCapability) -> BlockAuthoringPolicy:
    for policy, mapped in _POLICY_CAPABILITIES.items():
        if mapped == capability:
            return policy
    raise ValueError(f"No wire spelling for authoring capability {capability!r}")


DEFAULT_PROMPT_TEMPLATE = "workflow-copilot-agent.j2"
DEFAULT_MAX_TURNS = 200
DEFAULT_TOKEN_BUDGET = 90_000

SCREENSHOT_DROPPED_NUDGE = (
    "Your previous screenshot was dropped from context to recover from a token-budget overflow. "
    "Do NOT reason about the page from memory. Re-take the screenshot "
    "(get_browser_screenshot) or read the page again before deciding your next step."
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
    # Wire and persisted-metadata spelling of authoring_capability; read it only through that
    # property, which is what every authoring surface keys on.
    block_authoring_policy: BlockAuthoringPolicy = BlockAuthoringPolicy.TASK_V3_PURE
    code_block_available: bool = False
    effective_code_block_mode: bool = False
    # When False, this turn may neither dispatch runs nor acquire or drive a browser session.
    browser_tools_available: bool = True
    requested_output_path_aliases: dict[str, str] = field(default_factory=dict)
    requested_output_shape_expectations: dict[str, ShapeExpectation] = field(default_factory=dict)
    credential_pause_enabled: bool = field(default_factory=_default_credential_pause_enabled)
    credential_pause_timeout_seconds: int = field(default_factory=_default_credential_pause_timeout_seconds)

    @property
    def authoring_capability(self) -> AuthoringCapability:
        return authoring_capability_from_policy(self.block_authoring_policy)

    @authoring_capability.setter
    def authoring_capability(self, capability: AuthoringCapability) -> None:
        self.block_authoring_policy = block_authoring_policy_from_capability(capability)

    def nudge(self, key: str) -> str:
        return self.enforcement_nudges.get(key, DEFAULT_ENFORCEMENT_NUDGES[key])
