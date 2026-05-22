SECRET_HEADER_MASK = "***"


def mask_header_values(headers: dict[str, str] | None) -> dict[str, str] | None:
    """Replace every value in a header dict with ``SECRET_HEADER_MASK``.

    Used by Pydantic ``field_serializer`` hooks on ``cdp_connect_headers`` so that
    API responses never echo browser-provider credentials (e.g. ``x-api-key``)
    back to clients polling status endpoints. The serializer fires on
    ``model_dump``/JSON serialization only, so internal attribute access still
    returns the raw value used for the CDP handshake.
    """
    if not headers:
        return headers
    return {k: SECRET_HEADER_MASK for k in headers}


def merge_masked_headers(
    new_headers: dict[str, str] | None,
    stored_headers: dict[str, str] | None,
) -> dict[str, str] | None:
    """Resolve ``SECRET_HEADER_MASK`` entries in ``new_headers`` against ``stored_headers`` per-key.

    Masked entries fall back to the stored value (preserving credentials on round-trip)
    or are dropped when no stored value exists; non-masked entries pass through verbatim.
    Returns ``None`` only when ``new_headers`` is ``None`` so callers can distinguish
    "field omitted" from an explicit empty/cleared dict.
    """
    if new_headers is None:
        return None
    stored = stored_headers or {}
    merged: dict[str, str] = {}
    for key, value in new_headers.items():
        if value == SECRET_HEADER_MASK:
            if key in stored:
                merged[key] = stored[key]
            continue
        merged[key] = value
    return merged
