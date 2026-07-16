"""Shared minimal duck type for ``WorkflowRunContext`` in unit tests.

Used by branch-criteria / Jinja evaluation tests and while-loop integration tests
that call ``Block.format_block_parameter_template_from_workflow_run_context`` without
booting a full workflow runtime.
"""

from __future__ import annotations

from typing import Any


class FakeWorkflowRunContext:
    def __init__(
        self,
        *,
        values: dict[str, Any],
        secrets: dict[str, Any] | None = None,
        include_secrets_in_templates: bool = False,
        block_metadata: dict[str, dict[str, Any]] | None = None,
        workflow_run_outputs: dict[str, Any] | None = None,
    ) -> None:
        self.values = dict(values)
        self.secrets = secrets or {}
        self.include_secrets_in_templates = include_secrets_in_templates
        self._blocks_metadata: dict[str, dict[str, Any]] = {
            label: dict(meta) for label, meta in (block_metadata or {}).items()
        }

        self.workflow_title = "wf-title"
        self.workflow_id = "wf-id"
        self.workflow_permanent_id = "wf-perm-id"
        self.workflow_run_id = "wf-run-id"
        self.browser_session_id: str | None = None
        self.workflow_run_outputs: dict[str, Any] = dict(workflow_run_outputs or {})

    def get_block_metadata(self, label: str | None) -> dict[str, Any]:
        if not label:
            return {}
        return dict(self._blocks_metadata.get(label, {}))

    def update_block_metadata(self, label: str, metadata: dict[str, Any]) -> None:
        if label in self._blocks_metadata:
            self._blocks_metadata[label].update(metadata)
        else:
            self._blocks_metadata[label] = dict(metadata)

    def resolve_effective_workflow_system_prompt(self) -> str | None:
        return None

    def record_block_workflow_system_prompt(self, label: str, value: str | None) -> None:
        return None

    def has_value(self, key: str) -> bool:
        return key in self.values

    def set_value(self, key: str, value: Any) -> None:
        self.values[key] = value

    def build_workflow_run_summary(self) -> dict[str, Any]:
        return {}

    def mask_secrets_in_data(self, data: Any, mask: str = "*****") -> Any:
        return data
