import unicodedata
from urllib.parse import quote


def sanitize_header_filename(name: str) -> str:
    """Strip characters that would break or inject into a Content-Disposition header.

    The basename is derived from a user-controlled storage key. Rejects:

    - C0 (<0x20), DEL (0x7F), C1 (0x80-0x9F) — RFC 7230 violations.
    - ``"`` and ``\\`` — would terminate or escape the quoted value.
    - Unicode *format* (Cf) and *separator-line/paragraph* (Zl/Zp) chars —
      includes bidi overrides (U+202E), ZWSP (U+200B), ZWNBSP (U+FEFF);
      these enable filename spoofing in the browser download UI.
    """
    cleaned = []
    for ch in name:
        code = ord(ch)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            continue
        if ch in ('"', "\\"):
            continue
        if unicodedata.category(ch) in {"Cf", "Zl", "Zp"}:
            continue
        cleaned.append(ch)
    return "".join(cleaned) or "download"


def ascii_fallback_filename(name: str) -> str:
    """Best-effort ASCII form of ``name`` for the ``filename=`` parameter.

    NFKD-normalizes and drops combining marks first so accented Latin
    characters survive as their base letters (``fïlè.pdf`` → ``file.pdf``)
    instead of being stripped entirely. The RFC 5987 ``filename*=UTF-8''...``
    form still carries the full name for modern clients.

    If the ASCII stem ends up empty (e.g. pure CJK or emoji names),
    prepend ``download`` so legacy clients don't save a bare ``.pdf``
    hidden file.
    """
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    sanitized = sanitize_header_filename(ascii_only)
    stem, dot, ext = sanitized.rpartition(".")
    if dot and not stem:
        return f"download.{ext}"
    return sanitized


def build_attachment_disposition(filename: str) -> str:
    """Build a Content-Disposition header that survives non-ASCII filenames.

    Emits both ``filename="<ascii>"`` (for legacy clients) and
    ``filename*=UTF-8''<pct-encoded>`` (RFC 5987, for everything modern).
    Ensures the header value is Latin-1 encodable so Starlette doesn't 500.
    """
    safe = sanitize_header_filename(filename)
    ascii_part = ascii_fallback_filename(safe)
    encoded = quote(safe, safe="")
    return f"attachment; filename=\"{ascii_part}\"; filename*=UTF-8''{encoded}"
