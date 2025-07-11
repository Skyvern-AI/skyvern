from pydantic import BaseModel

from skyvern.schemas.runs import ProxyLocation


class LoginRequest(BaseModel):
    credential_id: str
    url: str | None = None
    prompt: str | None = None
    webhook_url: str | None = None
    proxy_location: ProxyLocation | None = None
    totp_identifier: str | None = None
    totp_url: str | None = None
    browser_session_id: str | None = None
    extra_http_headers: dict[str, str] | None = None
    max_screenshot_scrolling_times: int | None = None
