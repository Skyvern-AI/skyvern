from datetime import datetime

import structlog

LOG = structlog.get_logger()


def _parse_field(field: str, minimum: int, maximum: int) -> set[int]:
    """Parse a cron field into a set of integers."""
    values: set[int] = set()
    for part in field.split(","):
        if part == "*":
            values.update(range(minimum, maximum + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            values.update(range(minimum, maximum + 1, step))
        elif "-" in part:
            if "/" in part:
                rng, step_str = part.split("/")
                start, end = map(int, rng.split("-"))
                step = int(step_str)
                values.update(range(start, end + 1, step))
            else:
                start, end = map(int, part.split("-"))
                values.update(range(start, end + 1))
        else:
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
