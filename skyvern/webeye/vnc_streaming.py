from __future__ import annotations

from urllib.parse import urlparse

from skyvern.config import settings


def _browser_streaming_mode() -> str:
    return settings.BROWSER_STREAMING_MODE.strip().lower()


def is_local_vnc_streaming_enabled() -> bool:
    return _browser_streaming_mode() == "vnc"


def build_routed_vnc_url(browser_address: str | None) -> str | None:
    """
    Build a routed VNC URL from a V2 K8s routed browser address.

    V2 browser addresses look like:
    wss://{domain}/{session_id}/{token}/devtools/browser/{browser_id}
    """
    if not browser_address:
        return None

    parsed = urlparse(browser_address)
    if parsed.scheme not in ("wss", "ws"):
        return None

    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) < 4 or path_parts[2] != "devtools":
        return None

    session_id = path_parts[0]
    token = path_parts[1]
    scheme = "wss" if parsed.scheme == "wss" else "ws"
    return f"{scheme}://{parsed.netloc}/vnc/{session_id}/{token}"


def browser_session_supports_vnc_streaming(
    *,
    browser_address: str | None = None,
    ip_address: str | None = None,
) -> bool:
    return bool(
        build_routed_vnc_url(browser_address)
        or ip_address
        or is_local_vnc_streaming_enabled()
    )


def build_vnc_streaming_url(
    *,
    browser_address: str | None = None,
    ip_address: str | None = None,
    vnc_port: int,
) -> str | None:
    routed_vnc_url = build_routed_vnc_url(browser_address)
    if routed_vnc_url:
        return routed_vnc_url

    if ip_address:
        if ":" in ip_address:
            ip, _ = ip_address.split(":", maxsplit=1)
            return f"ws://{ip}:{vnc_port}"
        return f"ws://{ip_address}:{vnc_port}"

    if is_local_vnc_streaming_enabled():
        return f"ws://127.0.0.1:{vnc_port}"

    if browser_address:
        parsed_browser_address = urlparse(browser_address)
        if parsed_browser_address.hostname:
            return f"ws://{parsed_browser_address.hostname}:{vnc_port}"

    return None
