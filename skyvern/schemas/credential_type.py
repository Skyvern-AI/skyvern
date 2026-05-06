from enum import StrEnum


class CredentialType(StrEnum):
    skyvern = "skyvern"
    bitwarden = "bitwarden"
    onepassword = "1password"
    azure_vault = "azure_vault"
