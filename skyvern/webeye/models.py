from pydantic import BaseModel

from skyvern.webeye.browser_factory import BrowserState


class BrowserSessionResponse(BaseModel):
    session_id: str
    organization_id: str
