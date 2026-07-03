from __future__ import annotations

from typing import Any, cast

from skyvern.forge.sdk.db.utils import serialize_proxy_location
from skyvern.schemas.proxy_pinning import generate_proxy_session_id, should_generate_proxy_session_id
from skyvern.schemas.runs import ProxyLocation, ProxyLocationInput


def normalize_proxy_pin_for_create(
    *,
    proxy_location: ProxyLocationInput,
    proxy_session_id: str | None,
    entity_id: str | None = None,
) -> tuple[ProxyLocationInput, str | None]:
    if proxy_session_id and proxy_location is None:
        return ProxyLocation.RESIDENTIAL_ISP, proxy_session_id
    if proxy_location is None:
        return None, None
    if should_generate_proxy_session_id(proxy_location):
        if proxy_session_id is None and entity_id is not None:
            proxy_session_id = generate_proxy_session_id(entity_id)
        return proxy_location, proxy_session_id
    return proxy_location, None


def apply_proxy_pin_to_model(
    model: Any,
    *,
    entity_id: str,
    proxy_location: ProxyLocationInput | object,
    proxy_session_id: str | None | object,
    unset: object,
    rotate_proxy_session_id: bool = False,
) -> None:
    if proxy_location is not unset:
        model.proxy_location = serialize_proxy_location(cast(ProxyLocationInput, proxy_location))
        if proxy_location is None or not should_generate_proxy_session_id(proxy_location):
            model.proxy_session_id = None
        elif rotate_proxy_session_id or (proxy_session_id is unset and not model.proxy_session_id):
            model.proxy_session_id = generate_proxy_session_id(entity_id)

    if rotate_proxy_session_id and proxy_location is unset:
        if not should_generate_proxy_session_id(model.proxy_location):
            model.proxy_location = serialize_proxy_location(ProxyLocation.RESIDENTIAL_ISP)
        model.proxy_session_id = generate_proxy_session_id(entity_id)

    if proxy_session_id is not unset:
        if proxy_session_id:
            if proxy_location is unset:
                model.proxy_location = serialize_proxy_location(ProxyLocation.RESIDENTIAL_ISP)
            if proxy_location is unset or should_generate_proxy_session_id(proxy_location):
                model.proxy_session_id = cast(str, proxy_session_id)
        elif proxy_location is unset:
            model.proxy_location = None
            model.proxy_session_id = cast(str | None, proxy_session_id)
