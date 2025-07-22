from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from skyvern.schemas.runs import ProxyLocation


class CredentialType(StrEnum):
    skyvern = "skyvern"
    bitwarden = "bitwarden"
    onepassword = "1password"


class LoginRequestBase(BaseModel):
    url: str | None = Field(default=None, description="Website url")
    prompt: str | None = Field(
        default=None,
        description="Login instructions. Skyvern has default prompt/instruction for login if this field is not provided.",
    )
    webhook_url: str | None = Field(default=None, description="Webhook URL to send login status updates")
    proxy_location: ProxyLocation | None = Field(default=None, description="Proxy location to use")
    totp_identifier: str | None = Field(
        default=None, description="Identifier for TOTP (Time-based One-Time Password) if required"
    )
    totp_url: str | None = Field(default=None, description="TOTP URL to fetch one-time passwords")
    browser_session_id: str | None = Field(
        default=None, description="ID of the browser session to use, which is prefixed by `pbs_` e.g. `pbs_123456`"
    )
    extra_http_headers: dict[str, str] | None = Field(
        default=None, description="Additional HTTP headers to include in requests"
    )
    max_screenshot_scrolling_times: int | None = Field(
        default=None, description="Maximum number of times to scroll for screenshots"
    )


class SkyvernLoginRequest(LoginRequestBase):
    """
    Login with password saved in Skyvern
    """

    credential_type: Literal[CredentialType.skyvern] = CredentialType.skyvern
    credential_id: str = Field(..., description="ID of the Skyvern credential to use for login.")


class BitwardenLoginRequest(LoginRequestBase):
    """
    Login with password saved in Bitwarden
    """

    credential_type: Literal[CredentialType.bitwarden] = CredentialType.bitwarden
    bitwarden_collection_id: str | None = Field(default=None, description="Bitwarden collection ID")
    bitwarden_item_id: str | None = Field(default=None, description="Bitwarden item ID")


class OnePasswordLoginRequest(LoginRequestBase):
    """
    Login with password saved in 1Password
    """

    credential_type: Literal[CredentialType.onepassword] = CredentialType.onepassword
    onepassword_vault_id: str = Field(..., description="1Password vault ID.")
    onepassword_item_id: str = Field(..., description="1Password item ID.")


LoginRequest = Annotated[
    Union[SkyvernLoginRequest, BitwardenLoginRequest, OnePasswordLoginRequest],
    Field(discriminator="credential_type"),
]
