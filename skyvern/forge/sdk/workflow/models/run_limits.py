WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES = 4 * 60
WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES = 8 * 60


def reject_bool_max_elapsed_time_minutes(max_elapsed_time_minutes: object) -> object:
    if isinstance(max_elapsed_time_minutes, bool):
        raise ValueError("max_elapsed_time_minutes must be an integer, not a boolean")
    return max_elapsed_time_minutes


def get_effective_workflow_run_max_elapsed_time_minutes(max_elapsed_time_minutes: int | None) -> int:
    if max_elapsed_time_minutes is None:
        return WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES
    if not isinstance(max_elapsed_time_minutes, int) or isinstance(max_elapsed_time_minutes, bool):
        return WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES
    if max_elapsed_time_minutes <= 0:
        return WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES
    return min(max_elapsed_time_minutes, WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES)
