from datetime import datetime

import structlog

LOG = structlog.get_logger()


def _parse_field(field: str, minimum: int, maximum: int) -> set[int]:
    """Parse a cron field into a set of integers."""
    values: set[int] = set()
    rng_all = range(minimum, maximum + 1)
    for part in field.split(","):
        if part == "*":
            values.update(rng_all)
            continue
        # Handle step values, e.g., "*/5" or "2-10/3"
        if "/" in part:
            base, step_str = part.split("/")
            step = int(step_str)
            if base == "*":
                values.update(range(minimum, maximum + 1, step))
            elif "-" in base:
                start, end = map(int, base.split("-"))
                values.update(range(start, end + 1, step))
            else:
                # Unlikely for a cron field, treat as single value with step
                start = int(base)
                if minimum <= start <= maximum and (start - minimum) % step == 0:
                    values.add(start)
            continue
        # Handle ranges, e.g. "2-10"
        if "-" in part:
            start, end = map(int, part.split("-"))
            values.update(range(start, end + 1))
            continue
        # Handle single integer value; fast-path without using `set.add` for each part
        values.add(int(part))
    return values


def validate_cron_expression(expression: str) -> bool:
    """Return True if ``expression`` is a valid 5-field cron string."""
    try:
        fields = expression.split()
        if len(fields) != 5:
            return False
        _parse_field(fields[0], 0, 59)
        _parse_field(fields[1], 0, 23)
        _parse_field(fields[2], 1, 31)
        _parse_field(fields[3], 1, 12)
        _parse_field(fields[4], 0, 6)
    except Exception:
        return False
    return True


def cron_matches(now: datetime, expression: str) -> bool:
    """Check if ``now`` satisfies the cron ``expression``."""
    try:
        fields = expression.split()
        if len(fields) != 5:
            return False
        minute, hour, dom, month, dow = fields
        if now.minute not in _parse_field(minute, 0, 59):
            return False
        if now.hour not in _parse_field(hour, 0, 23):
            return False
        if now.day not in _parse_field(dom, 1, 31):
            return False
        if now.month not in _parse_field(month, 1, 12):
            return False
        weekday = (now.weekday() + 1) % 7
        if weekday not in _parse_field(dow, 0, 6):
            return False
        return True
    except Exception:
        LOG.exception("Invalid cron expression", expression=expression)
        return False
