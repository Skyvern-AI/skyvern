import random
import re
import string
import unicodedata
import uuid

RANDOM_STRING_POOL = string.ascii_letters + string.digits
# Module-level SystemRandom: draws from OS entropy without reseeding or
# mutating the shared global `random` state on every call.
_random = random.SystemRandom()


def generate_random_string(length: int = 5) -> str:
    return "".join(_random.choices(RANDOM_STRING_POOL, k=length))


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


def escape_code_fences(text: str | None, escape_quotes: bool = False) -> str:
    """Neutralize Markdown code-fence delimiters so fenced untrusted content
    can't break out of the fence and inject instructions. ``escape_quotes`` also
    rewrites ``"`` to ``'`` for values rendered inside a ``"..."`` literal
    (lossy, but harmless for the prompt).
    """
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", text)
    # Space out the whole run, not just the first three chars: replacing each
    # "```" in isolation leaves a trailing backtick that re-seams with the rest
    # of a longer run (e.g. 5 backticks), reforming an intact fence.
    text = re.sub(r"`{3,}|~{3,}", lambda m: " ".join(m.group()), text)
    if escape_quotes:
        text = text.replace('"', "'")
    return text
