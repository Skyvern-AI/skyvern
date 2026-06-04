from __future__ import annotations

from dataclasses import dataclass, field


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


@dataclass
class WorkflowVerificationEvidence:
    full_workflow_verified: bool = False
    block_verified: list[str] = field(default_factory=list)
    live_page_state_verified: bool = False
    test_attempted_but_incomplete: bool = False
    per_tool_budget_on_block: list[str] = field(default_factory=list)
    verified_from_current_browser_state: bool = False
    current_url_observed_after_workflow_run: bool = False
    current_url_may_encode_runtime_state: bool = False
    current_url: str | None = None
    page_title: str | None = None
    workflow_run_id: str | None = None
    unverified_block_labels: list[str] = field(default_factory=list)
    failed_block_labels: list[str] = field(default_factory=list)
    failure_reason: str | None = None

    def merge_verified_blocks(self, labels: list[str]) -> None:
        self.block_verified = _dedupe([*self.block_verified, *labels])

    def merge_per_tool_budget_blocks(self, labels: list[str]) -> None:
        self.per_tool_budget_on_block = _dedupe([*self.per_tool_budget_on_block, *labels])

    def has_evidence(self) -> bool:
        return bool(
            self.full_workflow_verified
            or self.block_verified
            or self.live_page_state_verified
            or self.test_attempted_but_incomplete
            or self.per_tool_budget_on_block
            or self.verified_from_current_browser_state
            or self.current_url_observed_after_workflow_run
            or self.current_url_may_encode_runtime_state
            or self.current_url
            or self.workflow_run_id
            or self.unverified_block_labels
            or self.failed_block_labels
            or self.failure_reason
        )

    def to_trace_data(self) -> dict[str, bool | int]:
        return {
            "full_workflow_verified": self.full_workflow_verified,
            "block_verified_count": len(self.block_verified),
            "live_page_state_verified": self.live_page_state_verified,
            "test_attempted_but_incomplete": self.test_attempted_but_incomplete,
            "per_tool_budget_on_block_count": len(self.per_tool_budget_on_block),
            "verified_from_current_browser_state": self.verified_from_current_browser_state,
            "current_url_observed_after_workflow_run": self.current_url_observed_after_workflow_run,
            "current_url_may_encode_runtime_state": self.current_url_may_encode_runtime_state,
            "has_current_url": bool(self.current_url),
            "has_workflow_run_id": bool(self.workflow_run_id),
            "unverified_block_count": len(self.unverified_block_labels),
            "failed_block_count": len(self.failed_block_labels),
            "has_failure_reason": bool(self.failure_reason),
        }

    def render_prompt_block(self) -> str:
        if not self.has_evidence():
            return ""

        lines = [
            f"full_workflow_verified: {str(self.full_workflow_verified).lower()}",
            f"live_page_state_verified: {str(self.live_page_state_verified).lower()}",
            f"test_attempted_but_incomplete: {str(self.test_attempted_but_incomplete).lower()}",
            f"verified_from_current_browser_state: {str(self.verified_from_current_browser_state).lower()}",
        ]
        if self.current_url_observed_after_workflow_run:
            lines.append("current_url_observed_after_workflow_run: true")
        if self.current_url_may_encode_runtime_state:
            lines.append("current_url_may_encode_runtime_state: true")
        for key, values in (
            ("block_verified", self.block_verified),
            ("per_tool_budget_on_block", self.per_tool_budget_on_block),
            ("unverified_block_labels", self.unverified_block_labels),
            ("failed_block_labels", self.failed_block_labels),
        ):
            if values:
                lines.append(f"{key}:")
                lines.extend(f"  - {value}" for value in values[:12])
        if self.current_url:
            lines.append(f"current_url: {self.current_url}")
        if self.page_title:
            lines.append(f"page_title: {self.page_title}")
        if self.workflow_run_id:
            lines.append(f"workflow_run_id: {self.workflow_run_id}")
        if self.failure_reason:
            lines.append(f"failure_reason: {self.failure_reason}")
        return "\n".join(lines)
