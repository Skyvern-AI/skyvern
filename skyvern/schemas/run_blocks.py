from enum import StrEnum

from pydantic import BaseModel, Field

from skyvern.schemas.runs import ProxyLocation


class CredentialType(StrEnum):
    skyvern = "skyvern"
    bitwarden = "bitwarden"
    onepassword = "1password"
    azure_vault = "azure_vault"


class LoginRequest(BaseModel):
    credential_type: CredentialType = Field(..., description="Where to get the credential from")
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
        default=None,
        description="ID of the browser session to use, which is prefixed by `pbs_` e.g. `pbs_123456`",
        examples=["pbs_123456"],
    )
    browser_address: str | None = Field(
        default=None,
        description="The CDP address for the task.",
        examples=["http://127.0.0.1:9222", "ws://127.0.0.1:9222/devtools/browser/1234567890"],
    )
    extra_http_headers: dict[str, str] | None = Field(
        default=None, description="Additional HTTP headers to include in requests"
    )
    max_screenshot_scrolling_times: int | None = Field(
        default=None, description="Maximum number of times to scroll for screenshots"
    )

    # Skyvern credential
    credential_id: str | None = Field(
        default=None, description="ID of the Skyvern credential to use for login.", examples=["cred_123"]
    )

    # Bitwarden credential
    bitwarden_collection_id: str | None = Field(
        default=None,
        description="Bitwarden collection ID. You can find it in the Bitwarden collection URL. e.g. `https://vault.bitwarden.com/vaults/collection_id/items`",
    )
    bitwarden_item_id: str | None = Field(default=None, description="Bitwarden item ID")

    # 1Password credential
    onepassword_vault_id: str | None = Field(default=None, description="1Password vault ID")
    onepassword_item_id: str | None = Field(default=None, description="1Password item ID")

    # Azure Vault credential
    azure_vault_name: str | None = Field(default=None, description="Azure Vault Name")
    azure_vault_username_key: str | None = Field(default=None, description="Azure Vault username key")
    azure_vault_password_key: str | None = Field(default=None, description="Azure Vault password key")
    azure_vault_totp_secret_key: str | None = Field(default=None, description="Azure Vault TOTP secret key")
