import asyncio
from typing import Annotated, Optional

import typer

from scripts.create_api_key import create_org_api_token
from skyvern.forge.app import DATABASE


async def create_org(org_name: str, webhook_callback_url: str | None = None) -> None:
    organization = await DATABASE.create_organization(org_name, webhook_callback_url)
    print(f"Created organization: {organization}")
    await create_org_api_token(organization.organization_id)


def main(
    org_name: str,
    webhook_callback_url: Annotated[Optional[str], typer.Argument()] = None,
) -> None:
    asyncio.run(create_org(org_name, webhook_callback_url))


if __name__ == "__main__":
    typer.run(main)
