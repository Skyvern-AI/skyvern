from enum import StrEnum


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


CUA_ENGINES = (RunEngine.openai_cua, RunEngine.anthropic_cua, RunEngine.ui_tars)
CUA_RUN_TYPES = (RunType.openai_cua, RunType.anthropic_cua, RunType.ui_tars)

# Statuses that are final; once a row reaches one of these, it never changes.
# Single source of truth used by sync cron, partial indexes, and run response helpers.
TERMINAL_STATUSES = ("completed", "failed", "terminated", "canceled", "timed_out")


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
        return self.value in TERMINAL_STATUSES
