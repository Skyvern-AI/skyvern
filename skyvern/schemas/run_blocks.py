from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from skyvern.schemas.runs import ProxyLocation


class CredentialType(StrEnum):
    skyvern = "skyvern"
    bitwarden = "bitwarden"
    onepassword = "1password"


class LoginRequestBase(BaseModel):
    url: str | None = None
    prompt: str | None = None
    webhook_url: str | None = None
    proxy_location: ProxyLocation | None = None
    totp_identifier: str | None = None
    totp_url: str | None = None
    browser_session_id: str | None = None
    extra_http_headers: dict[str, str] | None = None
    max_screenshot_scrolling_times: int | None = None


class SkyvernCredentialLoginRequest(LoginRequestBase):
    credential_type: Literal[CredentialType.skyvern] = CredentialType.skyvern
    credential_id: str


class BitwardenLoginRequest(LoginRequestBase):
    credential_type: Literal[CredentialType.bitwarden] = CredentialType.bitwarden
    collection_id: str | None = None
    item_id: str | None = None
    url: str | None = None


class OnePasswordLoginRequest(LoginRequestBase):
    credential_type: Literal[CredentialType.onepassword] = CredentialType.onepassword
    vault_id: str
    item_id: str


LoginRequest = Annotated[
    Union[SkyvernCredentialLoginRequest, BitwardenLoginRequest, OnePasswordLoginRequest],
    Field(discriminator="credential_type"),
]
