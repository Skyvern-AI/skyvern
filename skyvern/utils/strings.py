import os
import random
import re
import string
import uuid

RANDOM_STRING_POOL = string.ascii_letters + string.digits


def generate_random_string(length: int = 5) -> str:
    # Use the os.urandom(16) as the seed
    random.seed(os.urandom(16))
    return "".join(random.choices(RANDOM_STRING_POOL, k=length))


def is_uuid(string: str) -> bool:
    try:
        uuid.UUID(string)
        return True
    except ValueError:
        return False


def sanitize_identifier(value: str, default: str = "identifier") -> str:
    """Sanitizes a string to be a valid Python/Jinja2 identifier.

    Replaces non-alphanumeric characters (except underscores) with underscores,
    collapses consecutive underscores, strips leading/trailing underscores,
    and prepends an underscore if the result starts with a digit.

    Args:
        value: The raw value to sanitize.
        default: Fallback value if everything is stripped.

    Returns:
        A sanitized string that is a valid Python identifier.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", value)
    sanitized = re.sub(r"_+", "_", sanitized)
    sanitized = sanitized.strip("_")

    if sanitized and sanitized[0].isdigit():
        sanitized = "_" + sanitized

    if not sanitized:
        sanitized = default

    return sanitized
