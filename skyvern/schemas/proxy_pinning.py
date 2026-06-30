from __future__ import annotations

import json
import secrets
from typing import Any

from skyvern.schemas.proxy_location import GeoTarget, ProxyLocation, ProxyLocationInput

_LOWER_HEX = set("0123456789abcdef")


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


def redact_proxy_session_id(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 5:
        return "***"
    return f"{value[:3]}...{value[-2:]}"


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
