"""Dates typed as text, read the way a strict date input stores them (YYYY-MM-DD)."""

import re
from datetime import datetime

DATE_VALUE_SEPARATORS = re.compile(r"[^0-9]+")
DATE_MASK_SEPARATORS = re.compile(r"[^a-z]+")


def strict_date_mask_order(placeholder: str | None) -> tuple[str, ...] | None:
    # The day/month/year order a strict placeholder mask declares ("mm/dd/yyyy" -> ("m","d","y")), or None
    # when it is not a fully-specified mask: each separator-delimited token must be a pure run of one date
    # letter (d/dd, m/mm, yyyy), so prose, first-letter lookalikes, and partial years never define an order.
    if not placeholder:
        return None
    tokens = [token for token in DATE_MASK_SEPARATORS.split(placeholder.strip().lower()) if token]
    if len(tokens) != 3:
        return None
    order: list[str] = []
    for token in tokens:
        if re.fullmatch(r"d{1,2}", token):
            order.append("d")
        elif re.fullmatch(r"m{1,2}", token):
            order.append("m")
        elif re.fullmatch(r"y{4}", token):
            order.append("y")
        else:
            return None
    if sorted(order) != ["d", "m", "y"]:
        return None
    return tuple(order)


def canonical_iso_date(text: str, placeholder: str | None) -> str | None:
    # ``text`` as the YYYY-MM-DD an <input type=date> accepts, or None when it is not a date or the order
    # cannot be trusted. Order comes from the field's own strict mask; without a mask only an unambiguous
    # reading (four-digit year first, or a component above 12 pinning the day) is taken, and datetime()
    # rejects impossible calendar dates -- so an ambiguous value is refused, never written as a wrong date.
    parts = [part for part in DATE_VALUE_SEPARATORS.split(text.strip()) if part]
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    order = strict_date_mask_order(placeholder)
    if order is None:
        if len(parts[0]) == 4:
            order = ("y", "m", "d")
        elif len(parts[2]) == 4 and int(parts[0]) > 12:
            order = ("d", "m", "y")
        elif len(parts[2]) == 4 and int(parts[1]) > 12:
            order = ("m", "d", "y")
        else:
            return None
    fields = dict(zip(order, parts))
    if len(fields) != 3 or len(fields["y"]) != 4:
        return None
    try:
        return datetime(int(fields["y"]), int(fields["m"]), int(fields["d"])).strftime("%Y-%m-%d")
    except ValueError:
        return None
