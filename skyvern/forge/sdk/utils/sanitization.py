"""
Utility functions for sanitizing content before storing in the database.
"""

import re

import structlog

LOG = structlog.get_logger(__name__)
_SURROGATE_PATTERN = re.compile(r"[\ud800-\udfff]")


def sanitize_postgres_text(text: str) -> str:
    """
    Sanitize text to be stored in PostgreSQL by removing problematic characters.

    PostgreSQL text fields cannot contain:
    - NUL bytes (0x00)
    - Other problematic control characters and unpaired surrogates

    This function removes these characters while preserving normal whitespace.

    Args:
        text: The text to sanitize

    Returns:
        The sanitized text safe for PostgreSQL storage
    """
    if not text:
        return text

    original_length = len(text)

    # Remove NUL bytes (0x00) - PostgreSQL cannot store these
    sanitized = text.replace("\x00", "")

    # Remove other problematic control characters (0x01-0x08, 0x0B-0x0C, 0x0E-0x1F)
    # Keep common whitespace: \t (0x09), \n (0x0A), \r (0x0D)
    control_chars = (
        "".join(chr(i) for i in range(1, 9))
        + "".join(chr(i) for i in range(11, 13))
        + "".join(chr(i) for i in range(14, 32))
    )

    for char in control_chars:
        sanitized = sanitized.replace(char, "")

    removed_count = original_length - len(sanitized)
    if removed_count > 0:
        LOG.debug(
            "Removed problematic characters from text",
            original_length=original_length,
            removed_count=removed_count,
            sanitized_length=len(sanitized),
        )

    if not sanitized.isascii() and _SURROGATE_PATTERN.search(sanitized):
        sanitized = sanitized.encode("utf-16", "surrogatepass").decode("utf-16", "replace")

    return sanitized
