from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from rich.panel import Panel
from rich.prompt import Confirm

from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType

from .console import console
from .setup_commands import setup_claude, setup_claude_code, setup_cursor, setup_windsurf

if TYPE_CHECKING:
    from skyvern.forge.sdk.schemas.organizations import Organization

API_KEY_LIFETIME = timedelta(weeks=5200)
SKYVERN_LOCAL_ORG = "Skyvern-local"
SKYVERN_LOCAL_DOMAIN = "skyvern.local"


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _create_local_access_token(subject: str, expires_delta: timedelta) -> str:
    from skyvern.config import settings  # noqa: PLC0415

    if settings.SIGNATURE_ALGORITHM != "HS256":
        raise RuntimeError("Local quickstart token bootstrap only supports HS256 SIGNATURE_ALGORITHM")

    expire = datetime.utcnow() + expires_delta
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"exp": int(expire.timestamp()), "sub": str(subject)}
    signing_input = ".".join(
        [
            _base64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _base64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    )
    signature = hmac.new(settings.SECRET_KEY.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_base64url_encode(signature)}"


async def get_or_create_local_organization(database: Any | None = None) -> Organization:
    database = database or app.DATABASE
    organization = await database.organizations.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    if not organization:
        organization = await database.organizations.create_organization(
            organization_name=SKYVERN_LOCAL_ORG,
            domain=SKYVERN_LOCAL_DOMAIN,
            max_steps_per_run=10,
            max_retries_per_step=3,
        )
        api_key = _create_local_access_token(
            organization.organization_id,
            expires_delta=API_KEY_LIFETIME,
        )
        await database.organizations.create_org_auth_token(
            organization_id=organization.organization_id,
            token=api_key,
            token_type=OrganizationAuthTokenType.api,
        )
    return organization


async def setup_local_organization(database: Any | None = None) -> str:
    database = database or app.DATABASE
    organization = await get_or_create_local_organization(database=database)
    org_auth_token = await database.organizations.get_valid_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api.value,
    )
    return org_auth_token.token if org_auth_token else ""


async def setup_local_organization_from_database_string(database_string: str) -> str:
    """Seed the local org/API key without importing the full repository graph."""
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

    from skyvern.forge.sdk.db.id import generate_org_id, generate_organization_auth_token_id  # noqa: PLC0415

    connect_args = {"prepare_threshold": None} if "postgresql+psycopg" in database_string else {}
    engine = create_async_engine(database_string, connect_args=connect_args, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            organization_id = (
                await connection.execute(
                    text("SELECT organization_id FROM organizations WHERE domain = :domain LIMIT 1"),
                    {"domain": SKYVERN_LOCAL_DOMAIN},
                )
            ).scalar_one_or_none()

            if not organization_id:
                organization_id = generate_org_id()
                now = datetime.utcnow()
                await connection.execute(
                    text(
                        """
                        INSERT INTO organizations (
                            organization_id,
                            organization_name,
                            domain,
                            max_steps_per_run,
                            max_retries_per_step,
                            created_at,
                            modified_at
                        )
                        VALUES (
                            :organization_id,
                            :organization_name,
                            :domain,
                            :max_steps_per_run,
                            :max_retries_per_step,
                            :created_at,
                            :modified_at
                        )
                        """
                    ),
                    {
                        "organization_id": organization_id,
                        "organization_name": SKYVERN_LOCAL_ORG,
                        "domain": SKYVERN_LOCAL_DOMAIN,
                        "max_steps_per_run": 10,
                        "max_retries_per_step": 3,
                        "created_at": now,
                        "modified_at": now,
                    },
                )
                api_key = _create_local_access_token(
                    organization_id,
                    expires_delta=API_KEY_LIFETIME,
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO organization_auth_tokens (
                            id,
                            organization_id,
                            token_type,
                            token,
                            encrypted_token,
                            encrypted_method,
                            valid,
                            created_at,
                            modified_at
                        )
                        VALUES (
                            :id,
                            :organization_id,
                            :token_type,
                            :token,
                            :encrypted_token,
                            :encrypted_method,
                            :valid,
                            :created_at,
                            :modified_at
                        )
                        """
                    ),
                    {
                        "id": generate_organization_auth_token_id(),
                        "organization_id": organization_id,
                        "token_type": OrganizationAuthTokenType.api.value,
                        "token": api_key,
                        "encrypted_token": "",
                        "encrypted_method": "",
                        "valid": True,
                        "created_at": now,
                        "modified_at": now,
                    },
                )

            token = (
                await connection.execute(
                    text(
                        """
                        SELECT token
                        FROM organization_auth_tokens
                        WHERE organization_id = :organization_id
                          AND token_type = :token_type
                          AND valid = true
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "organization_id": organization_id,
                        "token_type": OrganizationAuthTokenType.api.value,
                    },
                )
            ).scalar_one_or_none()
            return token or ""
    finally:
        await engine.dispose()


def setup_mcp(
    *,
    local: bool = False,
    browser_type: str | None = None,
    browser_remote_debugging_url: str | None = None,
) -> None:
    console.print(Panel("[bold green]MCP Server Setup[/bold green]", border_style="green"))
    if local:
        console.print(
            "[italic]This configures local stdio MCP so Claude Code and other clients can talk directly to "
            "your localhost Skyvern server.[/italic]"
        )
    else:
        console.print("[italic]This configures Skyvern Cloud MCP in your AI tools.[/italic]")

    if Confirm.ask("Would you like to set up MCP integration for Claude Code?", default=True):
        setup_claude_code(
            api_key=None,
            dry_run=False,
            yes=True,
            local=local,
            use_python_path=True,
            url=None,
            project=False,
            global_config=False,
            skip_skills=False,
            browser_type=browser_type,
            browser_remote_debugging_url=browser_remote_debugging_url,
        )

    if Confirm.ask("Would you like to set up MCP integration for Claude Desktop?", default=True):
        setup_claude(
            api_key=None,
            dry_run=False,
            yes=True,
            local=local,
            use_python_path=True,
            url=None,
            browser_type=browser_type,
            browser_remote_debugging_url=browser_remote_debugging_url,
        )

    if Confirm.ask("Would you like to set up MCP integration for Cursor?", default=True):
        setup_cursor(
            api_key=None,
            dry_run=False,
            yes=True,
            local=local,
            use_python_path=True,
            url=None,
            browser_type=browser_type,
            browser_remote_debugging_url=browser_remote_debugging_url,
        )

    if Confirm.ask("Would you like to set up MCP integration for Windsurf?", default=True):
        setup_windsurf(
            api_key=None,
            dry_run=False,
            yes=True,
            local=local,
            use_python_path=True,
            url=None,
            browser_type=browser_type,
            browser_remote_debugging_url=browser_remote_debugging_url,
        )

    console.print("\n🎉 [bold green]MCP server configuration completed.[/bold green]")
