from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from skyvern.forge.taskv3.loop import ToolResult, ToolSpec

ApplicationContextState = Literal["yes_declared", "no_declared", "late_yes", "no_implicit", "not_asked"]

DECLARE_NOTICE = (
    "Never send declare_application_context by itself: in your first reply, call it together with your first page "
    "action (navigate or observe). is_job_application is true only if the task is to complete an employment "
    "application and applicant data is provided; otherwise false."
)
DECLARE_DESCRIPTION = (
    "Records whether this task is to complete an employment application with applicant data. Returns only a receipt. "
    "Send it alongside your first page action, in the same reply."
)


def payload_check_reasons(payload_qualified: bool, *, age_text_withheld: bool) -> tuple[str, str]:
    """The age and application-defaults reasons the payload check logs, read off a declaring run's one hook call."""
    if not payload_qualified:
        return "not_job_application", "not_job_application"
    return ("age_field_present" if age_text_withheld else "rendered"), "rendered"


@dataclass
class ApplicationContext:
    """The acting model's one yes/no on whether the run is a job application, and the text a yes unlocks."""

    defaults_text: str
    payload_qualified: bool = False
    state: ApplicationContextState | None = None
    delivered: bool = False
    asked: bool = False

    @property
    def final_state(self) -> ApplicationContextState:
        return self.state or ("no_implicit" if self.asked else "not_asked")

    @property
    def declared_yes(self) -> bool:
        return self.state in ("yes_declared", "late_yes")

    def audit_armed(self) -> bool:
        return self.payload_qualified or self.declared_yes

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="declare_application_context",
            description=DECLARE_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {"is_job_application": {"type": "boolean"}},
                "required": ["is_job_application"],
            },
            handler=self._declare,
        )

    def turn_notice(self, turn: int) -> str | None:
        if turn == 1:
            self.asked = True
            return DECLARE_NOTICE
        if self.state is None and self.asked:
            self.state = "no_implicit"
        # A user message and not the tool's result: delivered as a result, the text cost a turn and more failed runs.
        if self.declared_yes and not self.delivered:
            self.delivered = True
            return self.defaults_text
        return None

    async def _declare(self, args: dict[str, Any]) -> ToolResult:
        answer = args.get("is_job_application")
        if not isinstance(answer, bool):
            return ToolResult.error("Not recorded: is_job_application was not true or false.")
        if answer is True and not self.declared_yes:
            self.state = "yes_declared" if self.state is None else "late_yes"
        elif answer is False and self.state is None:
            self.state = "no_declared"
        return ToolResult.ok("Recorded.")
