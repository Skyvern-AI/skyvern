"""Structured context for copilot cross-turn memory."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.workflow.models.workflow import Workflow

ResponseType = Literal["REPLY", "ASK_QUESTION", "REPLACE_WORKFLOW"]

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.narration import NarratorState


class UrlVisit(BaseModel):
    url: str
    summary: str = ""


class FieldFilled(BaseModel):
    selector: str = ""
    label: str = ""
    value: str = ""


class CredentialCheck(BaseModel):
    credential_name: str = ""
    credential_id: str | None = None
    found: bool = False


class StructuredContext(BaseModel):
    user_goal: str = ""
    urls_visited: list[UrlVisit] = Field(default_factory=list)
    fields_filled: list[FieldFilled] = Field(default_factory=list)
    credentials_checked: list[CredentialCheck] = Field(default_factory=list)
    decisions_made: list[str] = Field(default_factory=list)
    workflow_state: str = ""

    def to_json_str(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json_str(cls, raw: str | None) -> StructuredContext:
        if not raw:
            return cls()
        raw = raw.strip()
        if raw.startswith("{"):
            try:
                return cls.model_validate_json(raw)
            except Exception:
                return cls(user_goal=raw)
        return cls(user_goal=raw)

    def merge_turn_summary(self, tool_activity: list[dict]) -> None:
        for entry in tool_activity:
            tool = entry.get("tool", "")
            summary = entry.get("summary", "")

            if tool == "navigate_browser":
                url = summary.removeprefix("Navigated to ").strip()
                if url and not any(v.url == url for v in self.urls_visited):
                    self.urls_visited.append(UrlVisit(url=url, summary=""))

            elif tool == "list_credentials":
                match = re.search(r"Found (\d+)", summary)
                found = int(match.group(1)) > 0 if match else False
                self.credentials_checked.append(CredentialCheck(credential_name=summary, found=found))

            elif tool == "type_text":
                parts = summary.split("into ")
                selector = parts[-1].strip("'\"") if len(parts) > 1 else ""
                # Intentionally omit value: typed text may contain PII / credentials.
                self.fields_filled.append(FieldFilled(selector=selector, label=selector))

            elif tool == "update_workflow":
                self.workflow_state = summary

            elif tool in ("click", "evaluate", "run_blocks_and_collect_debug", "get_run_results"):
                self.decisions_made.append(f"{tool}: {summary}")

            elif tool == "get_browser_screenshot":
                if "(" in summary and ")" in summary:
                    url = summary.split("(", 1)[1].rsplit(")", 1)[0]
                    if url and not any(v.url == url for v in self.urls_visited):
                        self.urls_visited.append(UrlVisit(url=url, summary="screenshot"))

            output = entry.get("output_preview")
            if output and tool in ("run_blocks_and_collect_debug", "get_run_results"):
                preview = output[:300] if len(output) > 300 else output
                self.decisions_made.append(f"  output: {preview}")

        if len(self.decisions_made) > 20:
            self.decisions_made = self.decisions_made[-15:]
        if len(self.urls_visited) > 50:
            self.urls_visited = self.urls_visited[-40:]
        if len(self.fields_filled) > 50:
            self.fields_filled = self.fields_filled[-40:]
        if len(self.credentials_checked) > 50:
            self.credentials_checked = self.credentials_checked[-40:]


@dataclass
class AgentResult:
    user_response: str
    updated_workflow: Workflow | None
    global_llm_context: str | None
    response_type: ResponseType = "REPLY"
    workflow_yaml: str | None = None
    workflow_was_persisted: bool = False
    # Feasibility-gate fast-path sets this True so the route can null any
    # previously-persisted proposed_workflow. Regular in-loop ASK_QUESTION
    # responses leave it False, preserving in-progress drafts.
    clear_proposed_workflow: bool = False
    # Actual API token usage accumulated across the agent run. None when no
    # provider reported usage on the stream — distinguishes "no data" from
    # "0 tokens" so eval cost grading can flag missing telemetry instead of
    # silently passing as cheap.
    total_tokens: int | None = None


@dataclass
class CopilotContext(AgentContext):
    """Unified context for the copilot agent run.

    Extends AgentContext with enforcement state, tool tracking, and
    workflow state needed by the SDK-based agent loop.

    Field-shadowing note: the enforcement / workflow / frontier state fields
    declared below are intentionally redeclared on this subclass. The parent
    ``AgentContext`` (in ``runtime.py``) still carries the same names with the
    same defaults for legacy paths that instantiate ``AgentContext`` directly.
    Python's MRO resolves to the child's declaration when a ``CopilotContext``
    instance is used — that's the desired behavior here. Stripping the
    duplicates from the parent is tracked in SKY-8974; until that lands, if
    you add a new field here, keep the defaults in sync with the parent to
    avoid drift.
    """

    workflow_copilot_chat_id: str | None = None

    # Enforcement state
    navigate_called: bool = False
    observation_after_navigate: bool = False
    navigate_enforcement_done: bool = False
    update_workflow_called: bool = False
    test_after_update_done: bool = False
    post_update_nudge_count: int = 0
    coverage_nudge_count: int = 0
    format_nudge_count: int = 0
    user_message: str = ""

    # Tool tracking
    consecutive_tool_tracker: list[str] = field(default_factory=list)
    tool_activity: list[dict[str, Any]] = field(default_factory=list)

    # ``None`` until usage is observed; ``0`` only when a provider explicitly
    # reported zero. Distinct values let cost grading flag missing telemetry.
    total_tokens_used: int | None = None
    input_tokens_used: int | None = None
    output_tokens_used: int | None = None

    # Workflow state
    last_workflow: Workflow | None = None
    last_workflow_yaml: str | None = None
    workflow_persisted: bool = False
    last_update_block_count: int | None = None
    last_test_ok: bool | None = None
    last_test_failure_reason: str | None = None
    failed_test_nudge_count: int = 0
    explore_without_workflow_nudge_count: int = 0
    last_failed_workflow_yaml: str | None = None
    # Set when a block-running tool timed out and the run's true outcome
    # could not be reconciled (post-drain row was ``canceled``, non-final, or
    # unreadable). Blocks further block-running tool calls until the LLM
    # calls ``get_run_results(workflow_run_id=<same>)`` AND that read returns
    # a status in ``_TRUSTED_POST_DRAIN_STATUSES``. Turn-scoped by
    # construction — ``CopilotContext`` is re-created per agent turn — so
    # this guards auto-retry WITHIN a turn but not cross-turn "user says
    # retry" requests.
    pending_reconciliation_run_id: str | None = None
    # Consecutive test runs whose data-producing blocks completed with no
    # meaningful output (missing, empty, or all-null fields). Resets when a
    # run produces real data. Used to escalate when the agent is stuck
    # retrying extraction against a page that doesn't contain the data.
    null_data_streak_count: int = 0
    # Consecutive failed runs where navigation completed but the scraper
    # could not read the page (generic "failed to load the website" template).
    # Resets on any non-matching run outcome. Streak crosses workflow-shape
    # changes deliberately — the frontier fingerprint resets each time the
    # copilot rewrites the workflow, but the underlying site-block pattern is
    # shape-independent.
    probable_site_block_streak_count: int = 0

    # Per-request frontier state. `verified_block_outputs` and
    # `verified_prefix_labels` are populated ONLY from fully-successful runs —
    # a single failed block in the executed suffix leaves the prior verified
    # state untouched, because the browser session is now in post-failure
    # state and the prefix labels can no longer be trusted as an anchor.
    verified_block_outputs: dict[str, Any] = field(default_factory=dict)
    verified_prefix_labels: list[str] = field(default_factory=list)
    last_requested_block_labels: list[str] = field(default_factory=list)
    last_executed_block_labels: list[str] = field(default_factory=list)
    last_frontier_start_label: str | None = None
    last_frontier_fingerprint: str | None = None
    last_failure_signature: str | None = None
    repeated_failure_streak_count: int = 0
    # Highest streak level at which we've already emitted a repeated-failure
    # nudge. Prevents the warn nudge from re-firing every turn while the
    # streak is still at 2, and guarantees the stop nudge fires exactly once
    # when the streak reaches 3.
    repeated_failure_nudge_emitted_at_streak: int = 0
    # Set by _record_run_blocks_result when the most recent failed run matches
    # SKIP_INNER_NAV_RETRY_ERRORS (DNS / cert / SSL / invalid URL). Drives the
    # one-shot non-retriable-nav stop nudge and the deterministic exit-path
    # exception in run_with_enforcement. Cleared at the top of every call to
    # _record_run_blocks_result so stale state can't leak across runs.
    last_test_non_retriable_nav_error: str | None = None
    # Normalized signature of the non-retriable nav error last nudged on.
    # Lets the stop nudge re-fire if the user retries with a different bad URL
    # (different signature) in the same session. Cleared on meaningful success.
    non_retriable_nav_error_last_emitted_signature: str | None = None
    last_failure_category_top: str | None = None
    # Hash of the ordered (action_type, element_id) tuples from the last run's
    # action trace. When the same fingerprint repeats run-over-run with no
    # intervening success, the agent is stuck re-firing the same clicks/inputs —
    # typically because a captcha/popup/anti-bot is blocking progress. The
    # streak counter drives the hard-abort short-circuit in _tool_loop_error.
    # ``pending_action_sequence_fingerprint`` holds the fingerprint of the run
    # that JUST completed, computed by ``_run_blocks_and_collect_debug`` before
    # action_trace is stripped. ``update_repeated_failure_state`` compares it
    # to ``last_action_sequence_fingerprint`` (the prior run's fingerprint),
    # updates the streak, then promotes pending → last.
    last_action_sequence_fingerprint: str | None = None
    pending_action_sequence_fingerprint: str | None = None
    repeated_action_fingerprint_streak_count: int = 0

    # Populated lazily by ``stream_to_sse`` and reused across enforcement
    # iterations so cadence/last-emitted-at survive ``run_with_enforcement``
    # retries. Declared here (rather than attached dynamically) so future
    # refactors can't strip it silently.
    narrator_state: NarratorState | None = None
