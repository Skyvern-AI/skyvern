from typing import Annotated

from pydantic import AfterValidator

from skyvern.constants import MAX_SCREENSHOT_SCROLLS

WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES = 4 * 60
WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES = 8 * 60


def clamp_max_screenshot_scrolls(value: int | None) -> int | None:
    """Clamp a scroll count into ``[0, MAX_SCREENSHOT_SCROLLS]``.

    An *after*-validator so it runs once Pydantic has coerced the input to ``int``
    — clamping a stringified number (e.g. ``"2147483648"``) as well as a raw int.
    It normalizes both incoming requests (a value above the INTEGER column limit
    would overflow ``max_screenshot_scrolling_times`` on insert) and rows hydrated
    from the DB — a pre-cap historical value must not fail validation on
    read/response reconstruction.
    """
    if value is None:
        return None
    return max(0, min(value, MAX_SCREENSHOT_SCROLLS))


MaxScreenshotScrolls = Annotated[int | None, AfterValidator(clamp_max_screenshot_scrolls)]


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
