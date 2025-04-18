"""
Utility functions for sanitizing content before storing in the database.
"""


def sanitize_postgres_text(text: str) -> str:
    """
    Sanitize text to be stored in PostgreSQL by removing NUL bytes.

    PostgreSQL text fields cannot contain NUL (0x00) bytes, so we remove them.

    Args:
        text: The text to sanitize

    Returns:
        The sanitized text without NUL bytes
    """
    return text.replace("\0", "")
