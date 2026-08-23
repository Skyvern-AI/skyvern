from __future__ import annotations

import hmac
import json
import re
import secrets
from hashlib import sha256
from typing import Any

from skyvern.schemas.proxy_location import GeoTarget, ProxyLocation, ProxyLocationInput

_LOWER_HEX = set("0123456789abcdef")
# Nothing in this class can form a scheme, a userinfo or an escape, so matching it proves the
# value holds no credential. The length bound comes from the set it exists to admit: the longest
# ProxyLocation value is 15 characters, and the margin covers a typo of one. Wider than that and
# the class starts admitting token-shaped strings, which is the shape it is meant to exclude.
_ENUM_SHAPED_RE = re.compile(r"[A-Za-z0-9_-]{1,20}")
# Keyed per process: an unkeyed digest of a value holding a low-entropy password is an
# offline verifier, since the surrounding JSON is a known template and candidates can be
# hashed until one matches. The key never leaves memory, so identifiers correlate within a
# process's logs and cannot be recomputed from a guess.
_LOG_DIGEST_KEY = secrets.token_bytes(16)


def is_proxy_session_id(value: str) -> bool:
    return len(value) == 10 and all(char in _LOWER_HEX for char in value)


def normalize_proxy_session_id(value: str | None) -> str | None:
    if value is None:
        return None
    raw_value = value.strip()
    if not raw_value:
        return None
    return raw_value


def generate_proxy_session_id(source_id: str) -> str:
    if normalize_proxy_session_id(source_id) is None:
        raise ValueError("Cannot generate proxy session id from an empty entity id")
    proxy_session_id = secrets.token_hex(5)
    if not is_proxy_session_id(proxy_session_id):
        raise RuntimeError("Generated proxy session id does not match the expected format")
    return proxy_session_id


def derive_proxy_session_id(*parts: str) -> str:
    if not parts or any(not part or not part.strip() for part in parts):
        raise ValueError("Cannot derive proxy session id from empty parts")
    return sha256(":".join(parts).encode("utf-8")).hexdigest()[:10]


def redact_proxy_session_id(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 5:
        return "***"
    return f"{value[:3]}...{value[-2:]}"


def _identify(value: object) -> str:
    """A keyed identifier for a value that must not be rendered."""
    return hmac.new(_LOG_DIGEST_KEY, repr(value).encode("utf-8", "replace"), sha256).hexdigest()[:12]


def _form_of(value: object) -> str:
    """The kind of proxy_location this is, from its type and key names only - never its values."""
    if value is None:
        return "none"
    if isinstance(value, dict):
        return "custom_url" if "url" in value else "geo_dict"
    if isinstance(value, str):
        return "json" if value.lstrip()[:1] == "{" else "string"
    return type(value).__name__


def redact_proxy_location(value: object) -> str:
    """A proxy_location named for a log line, never rendered into one.

    Only values a type or a closed character class proves cannot hold a credential are printed:
    the enum, a validated GeoTarget, and an enum-shaped string. Everything else is named and given
    a keyed identifier, because a mechanism that renders a value first and sanitises it afterwards
    has an unbounded set of placements and encodings to be defeated by, and sixteen review findings
    on this helper were sixteen draws from that set.

    The identifier is an HMAC under a key generated once per process and never written down: it
    correlates two log lines within one process and cannot confirm a guess. Nothing about the
    value's length or contents is emitted alongside it.
    """
    if isinstance(value, ProxyLocation):
        return value.value
    if isinstance(value, GeoTarget):
        # NOT safe wholesale: city takes 100 characters of free text and subdivision 10, so a
        # validated GeoTarget can carry a URL. country is pinned to a supported set, so it is the
        # only field of it that can be shown.
        return f"geo_target:{value.country}:{_identify(value)}"
    if isinstance(value, str) and _ENUM_SHAPED_RE.fullmatch(value):
        return value
    return f"{_form_of(value)}:{_identify(value)}"


def should_generate_proxy_session_id(proxy_location: object | None) -> bool:
    return proxy_location == ProxyLocation.RESIDENTIAL_ISP or proxy_location == ProxyLocation.RESIDENTIAL_ISP.value


def apply_proxy_pin_update(
    update_kwargs: dict[str, Any],
    *,
    proxy_location_was_set: bool,
    proxy_location: ProxyLocationInput,
    proxy_session_id_was_set: bool,
    proxy_session_id: str | None,
    rotate_proxy_session_id: bool = False,
) -> None:
    if rotate_proxy_session_id:
        update_kwargs["rotate_proxy_session_id"] = True

    if proxy_location_was_set:
        update_kwargs["proxy_location"] = proxy_location
        if proxy_location is None or not should_generate_proxy_session_id(proxy_location):
            update_kwargs["proxy_session_id"] = None
        elif proxy_session_id_was_set and proxy_session_id is not None:
            update_kwargs["proxy_session_id"] = proxy_session_id
        return

    if proxy_session_id_was_set:
        if proxy_session_id:
            update_kwargs["proxy_location"] = ProxyLocation.RESIDENTIAL_ISP
            update_kwargs["proxy_session_id"] = proxy_session_id
        else:
            update_kwargs["proxy_location"] = None
            update_kwargs["proxy_session_id"] = None


def validate_proxy_session_id(value: str | None) -> str | None:
    return normalize_proxy_session_id(value)


def parse_proxy_location_input(value: object) -> object:
    if value is None or isinstance(value, (ProxyLocation, GeoTarget)):
        return value
    if isinstance(value, dict):
        if "url" in value and "country" not in value:
            raise ValueError("Custom proxy URLs are not supported for pinned proxy identities")
        return GeoTarget.model_validate(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.startswith("{"):
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("proxy_location JSON must be an object")
            return parse_proxy_location_input(data)
        return ProxyLocation(raw)
    return value
