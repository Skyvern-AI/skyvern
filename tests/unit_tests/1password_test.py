import asyncio
import os

from onepassword.client import Client


async def main():
    # Get service account token
    token = os.getenv("OP_SERVICE_ACCOUNT_TOKEN")
    if not token:
        raise EnvironmentError("OP_SERVICE_ACCOUNT_TOKEN not set")

    # Authenticate client
    client = await Client.authenticate(auth=token, integration_name="simple-login-access", integration_version="1.0.0")

    # Vault ID for test_vault (you confirmed this earlier)
    vault_id = "hdqnc4iwajd63vc6iuvy24zzqa"

    # Find the item titled "Login"
    items = await client.items.list(vault_id=vault_id)
    login_item = next((item for item in items if item.title == "login"), None)

    if not login_item:
        raise Exception("Login item not found in test_vault")

    item_id = login_item.id

    # Build references
    username_ref = f"op://{vault_id}/{item_id}/username"
    password_ref = f"op://{vault_id}/{item_id}/password"

    print(f"Username reference: {username_ref}")
    print(f"Password reference: {password_ref}")

    # Fetch secrets
    secrets = await client.secrets.resolve_all([username_ref, password_ref])
    username = secrets.individual_responses[username_ref].content.secret
    password = secrets.individual_responses[password_ref].content.secret

    # Print results
    print("✅ Retrieved login credentials:")
    print(f"- Username: {username}")
    print(f"- Password: {password}")


if __name__ == "__main__":
    asyncio.run(main())
