import os

from onepassword.client import Client


async def resolve_secret(reference: str) -> str:
    token = os.getenv("OP_SERVICE_ACCOUNT_TOKEN")
    client = await Client.authenticate(
        auth=token,
        integration_name="Skyvern 1Password",
        integration_version="v1.0.0",
    )
    return await client.secrets.resolve(reference)
