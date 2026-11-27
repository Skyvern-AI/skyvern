from typing import Any, Protocol

BROWSER_DOWNLOAD_WILL_BEGIN = "Browser.downloadWillBegin"
BROWSER_DOWNLOAD_PROGRESS = "Browser.downloadProgress"


class BrowserDownloadSession(Protocol):
    async def send(self, method: str, params: dict[str, Any]) -> dict: ...


async def set_browser_download_behavior(
    session: BrowserDownloadSession,
    behavior: str,
    *,
    download_path: str | None = None,
    browser_context_id: str | None = None,
) -> None:
    params: dict[str, Any] = {"behavior": behavior, "eventsEnabled": True}
    if download_path is not None:
        params["downloadPath"] = download_path
    if browser_context_id is not None:
        params["browserContextId"] = browser_context_id
    await session.send("Browser.setDownloadBehavior", params)
