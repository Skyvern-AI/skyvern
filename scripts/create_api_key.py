from dotenv import load_dotenv
import os
load_dotenv()
print("DATABASE_URL:", os.environ.get("DATABASE_URL"))

import sys
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import asyncio
from datetime import timedelta
import typer
from sqlalchemy.exc import SQLAlchemyError

from skyvern.forge.app import DATABASE
from skyvern.forge.sdk.core import security
from skyvern.forge.sdk.schemas.organizations import OrganizationAuthToken, OrganizationAuthTokenType

API_KEY_LIFETIME = timedelta(weeks=5200)

async def check_db_connection():
    print("Checking database connectivity...")
    try:
        import sqlalchemy
        async with DATABASE.engine.connect() as conn:
            await conn.execute(sqlalchemy.text("SELECT 1"))
        print("Database connection successful.")
    except Exception as e:
        print(f"Database connection failed: {e}")
        print("Check your .env file and database server.")
        sys.exit(1)

def check_migrations():
    print("Checking for pending migrations...")
    try:
        from alembic.config import Config
        from alembic import command
        alembic_cfg = Config(os.path.join(os.path.dirname(__file__), '../alembic.ini'))
        # This will print the current revision, not a full check, but will error if migrations are missing
        command.current(alembic_cfg)
        print("Alembic migrations appear to be present.")
    except Exception as e:
        print(f"Alembic migration check failed: {e}")
        print("Try running: alembic upgrade head")
        sys.exit(1)

async def check_organization_exists(org_id: str):
    print(f"Checking if organization '{org_id}' exists...")
    organization = await DATABASE.get_organization(org_id)
    if not organization:
        print(f"Organization id '{org_id}' not found in the database.")
        print("You may need to create it first. Try running: python scripts/create_organization.py <org_id>")
        sys.exit(1)
    print(f"Organization '{org_id}' exists.")
    return organization

async def create_org_api_token(org_id: str) -> OrganizationAuthToken:
    print("Creating API token...")
    organization = await check_organization_exists(org_id)
    api_key = security.create_access_token(
        org_id,
        expires_delta=API_KEY_LIFETIME,
    )
    try:
        org_auth_token = await DATABASE.create_org_auth_token(
            organization_id=org_id,
            token=api_key,
            token_type=OrganizationAuthTokenType.api,
        )
        print(f"Created API token for organization: {org_auth_token.token}")
        return org_auth_token
    except SQLAlchemyError as e:
        print(f"Failed to create API token: {e}")
        sys.exit(1)

async def main_async(org_id: str) -> None:
    await check_db_connection()
    check_migrations()
    await create_org_api_token(org_id)

def main(org_id: str) -> None:
    asyncio.run(main_async(org_id))

if __name__ == "__main__":
    typer.run(main)
