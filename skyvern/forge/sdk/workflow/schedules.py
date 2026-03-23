from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import structlog
from croniter import croniter  # type: ignore[import-untyped]

LOG = structlog.get_logger()

MIN_CRON_INTERVAL_SECONDS = 5 * 60


def validate_timezone_name(timezone: str) -> None:
    try:
        ZoneInfo(timezone)
    except Exception as e:  # ZoneInfoNotFoundError derives from KeyError
        raise ValueError(f"Invalid timezone '{timezone}'") from e


def validate_cron_expression(cron_expression: str, minimum_interval_seconds: int = MIN_CRON_INTERVAL_SECONDS) -> None:
    if not croniter.is_valid(cron_expression):
        raise ValueError("Invalid cron expression")

    now = datetime.now(UTC)
    cron = croniter(cron_expression, now)
    first = cron.get_next(datetime)
    second = cron.get_next(datetime)
    if (second - first).total_seconds() < minimum_interval_seconds:
        raise ValueError(f"Cron interval must be at least {minimum_interval_seconds // 60} minutes")


def calculate_next_runs(cron_expression: str, timezone: str, count: int) -> list[datetime]:
    now = datetime.now(ZoneInfo(timezone))
    itr = croniter(cron_expression, now)
    return [itr.get_next(datetime).astimezone(UTC) for _ in range(count)]


def compute_next_run(cron_expression: str, timezone: str) -> datetime:
    """Compute the single next run time. Caller must ensure inputs are valid (e.g. from DB)."""
    now = datetime.now(ZoneInfo(timezone))
    itr = croniter(cron_expression, now)
    return itr.get_next(datetime).astimezone(UTC)


def compute_previous_fire_time(cron_expression: str, timezone: str) -> datetime:
    """Compute the most recent scheduled fire time. Caller must ensure inputs are valid (e.g. from DB)."""
    now = datetime.now(ZoneInfo(timezone))
    itr = croniter(cron_expression, now)
    return itr.get_prev(datetime).astimezone(UTC)
