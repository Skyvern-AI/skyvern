from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import structlog
from croniter import croniter  # type: ignore[import-untyped]

LOG = structlog.get_logger()

# Mirrored in the frontend's cronUtils.ts (MIN_CRON_INTERVAL_SECONDS,
# meetsMinCronInterval) — keep both copies of these three constants in sync.
MIN_CRON_INTERVAL_SECONDS = 5 * 60


def validate_timezone_name(timezone: str) -> None:
    try:
        ZoneInfo(timezone)
    except Exception as e:  # ZoneInfoNotFoundError derives from KeyError
        raise ValueError(f"Invalid timezone '{timezone}'") from e


# Sample a full day of firings so the minimum-gap check can't be bypassed by a
# tight cluster that falls outside a small fixed sample (e.g.
# "0,5,...,55,59 * * * *" hides the 55->59 and 59->00 gaps from a 10-run window).
# A 25h span covers any minute/hour-field cycle (incl. the hour wraparound); the
# count cap bounds dense crons like "*/1 * * * *".
CRON_INTERVAL_SAMPLE_WINDOW_SECONDS = 25 * 60 * 60
CRON_INTERVAL_MAX_SAMPLES = 2000


def validate_cron_expression(cron_expression: str, minimum_interval_seconds: int = MIN_CRON_INTERVAL_SECONDS) -> None:
    if not croniter.is_valid(cron_expression):
        raise ValueError("Invalid cron expression")

    now = datetime.now(UTC)
    cron = croniter(cron_expression, now)
    runs = [cron.get_next(datetime)]
    while len(runs) < CRON_INTERVAL_MAX_SAMPLES:
        runs.append(cron.get_next(datetime))
        if (runs[-1] - runs[0]).total_seconds() >= CRON_INTERVAL_SAMPLE_WINDOW_SECONDS:
            break
    min_gap_seconds = min((runs[i + 1] - runs[i]).total_seconds() for i in range(len(runs) - 1))
    if min_gap_seconds < minimum_interval_seconds:
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
