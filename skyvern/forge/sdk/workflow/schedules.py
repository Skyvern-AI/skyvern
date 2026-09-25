from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from croniter import croniter  # type: ignore[import-untyped]

LOG = structlog.get_logger()

# Mirrored in the frontend's cronUtils.ts (MIN_SCHEDULE_INTERVAL_SECONDS,
# meetsMinCronInterval) — keep both copies of these three constants in sync.
MIN_SCHEDULE_INTERVAL_SECONDS = 5 * 60
# Sample a full day of firings so the minimum-gap check can't be bypassed by a
# tight cluster that falls outside a small fixed sample (e.g.
# "0,5,...,55,59 * * * *" hides the 55->59 and 59->00 gaps from a 10-run window).
# A 25h span covers any minute/hour-field cycle (incl. the hour wraparound); the
# count cap bounds dense crons like "*/1 * * * *".
CRON_INTERVAL_SAMPLE_WINDOW_SECONDS = 25 * 60 * 60
CRON_INTERVAL_MAX_SAMPLES = 2000


def validate_timezone_name(timezone: str) -> None:
    try:
        ZoneInfo(timezone)
    except Exception as e:  # ZoneInfoNotFoundError derives from KeyError
        raise ValueError(f"Invalid timezone '{timezone}'") from e


def validate_cron_expression(
    cron_expression: str, minimum_interval_seconds: int = MIN_SCHEDULE_INTERVAL_SECONDS
) -> None:
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


MAX_INTERVAL_SECONDS = 2**31 - 1
NEXT_RUNS_COUNT = 5
# Bounded so NEXT_RUNS_COUNT ticks of any allowed interval stay inside datetime's range.
LATEST_FIRST_FIRE_AT = (datetime.max - timedelta(seconds=NEXT_RUNS_COUNT * MAX_INTERVAL_SECONDS)).replace(
    microsecond=0, tzinfo=UTC
)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def default_first_fire_at(interval_seconds: int) -> datetime:
    return datetime.fromtimestamp(math.floor(datetime.now(UTC).timestamp()) + interval_seconds, UTC)


def _interval_tick_index(interval_seconds: int, first_fire_at: datetime) -> tuple[int, int]:
    """Return (anchor_epoch, k) where anchor + k*N is the latest tick at or before now; k is -1 before the anchor."""
    anchor = int(as_utc(first_fire_at).timestamp())
    now = math.floor(datetime.now(UTC).timestamp())
    return anchor, (now - anchor) // interval_seconds if now >= anchor else -1


def _interval_tick(anchor: int, interval_seconds: int, k: int) -> datetime:
    return datetime.fromtimestamp(anchor + k * interval_seconds, UTC)


def calculate_next_runs(
    cron_expression: str | None,
    timezone: str,
    count: int,
    *,
    interval_seconds: int | None = None,
    first_fire_at: datetime | None = None,
) -> list[datetime]:
    if interval_seconds is not None and first_fire_at is not None:
        anchor, k = _interval_tick_index(interval_seconds, first_fire_at)
        return [_interval_tick(anchor, interval_seconds, k + 1 + i) for i in range(count)]
    if cron_expression is None:
        raise ValueError("Schedule has neither a cron expression nor an interval")
    now = datetime.now(ZoneInfo(timezone))
    itr = croniter(cron_expression, now)
    return [itr.get_next(datetime).astimezone(UTC) for _ in range(count)]


def compute_next_run(
    cron_expression: str | None,
    timezone: str,
    *,
    interval_seconds: int | None = None,
    first_fire_at: datetime | None = None,
) -> datetime:
    """Compute the single next run time. Caller must ensure inputs are valid (e.g. from DB)."""
    return calculate_next_runs(
        cron_expression, timezone, 1, interval_seconds=interval_seconds, first_fire_at=first_fire_at
    )[0]


def compute_previous_fire_time(
    cron_expression: str | None,
    timezone: str,
    *,
    interval_seconds: int | None = None,
    first_fire_at: datetime | None = None,
) -> datetime | None:
    """Compute the most recent scheduled fire time, or None when an interval has not reached its anchor yet."""
    if interval_seconds is not None and first_fire_at is not None:
        anchor, k = _interval_tick_index(interval_seconds, first_fire_at)
        return _interval_tick(anchor, interval_seconds, k) if k >= 0 else None
    if cron_expression is None:
        raise ValueError("Schedule has neither a cron expression nor an interval")
    now = datetime.now(ZoneInfo(timezone))
    itr = croniter(cron_expression, now)
    return itr.get_prev(datetime).astimezone(UTC)
