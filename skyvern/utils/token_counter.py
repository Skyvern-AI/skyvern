import math

import structlog
import tiktoken

LOG = structlog.get_logger()

_APPROX_CHARS_PER_TOKEN = 4

_encoding: tiktoken.Encoding | None = None
_encoding_load_failed = False


def _get_encoding() -> tiktoken.Encoding | None:
    """Load the gpt-4o encoding lazily. tiktoken may download the encoding from a
    remote host on first use; in environments without egress that raises, so a
    failure is cached and callers fall back to a character-based approximation
    rather than crashing."""
    global _encoding, _encoding_load_failed
    if _encoding is not None or _encoding_load_failed:
        return _encoding
    try:
        _encoding = tiktoken.encoding_for_model("gpt-4o")
    except Exception:
        _encoding_load_failed = True
        LOG.warning("Failed to load tiktoken encoding; falling back to approximate token counting", exc_info=True)
    return _encoding


def count_tokens(text: str) -> int:
    encoding = _get_encoding()
    if encoding is None:
        return math.ceil(len(text) / _APPROX_CHARS_PER_TOKEN)
    return len(encoding.encode(text))


def encode_tokens(text: str) -> list[int]:
    encoding = _get_encoding()
    if encoding is None:
        return [ord(char) for char in text]
    return encoding.encode(text)


def decode_tokens(tokens: list[int]) -> str:
    encoding = _get_encoding()
    if encoding is None:
        return "".join(chr(token) for token in tokens)
    return encoding.decode(tokens)
