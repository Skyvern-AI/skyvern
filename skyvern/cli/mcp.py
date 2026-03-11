from rich.panel import Panel
from rich.prompt import Confirm

from skyvern.forge import app
from skyvern.forge.sdk.core import security
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services.local_org_auth_token_service import SKYVERN_LOCAL_DOMAIN, SKYVERN_LOCAL_ORG
from skyvern.forge.sdk.services.org_auth_token_service import API_KEY_LIFETIME

from .console import console
from .setup_commands import setup_claude, setup_claude_code, setup_cursor, setup_windsurf


async def get_or_create_local_organization() -> Organization:
    organization = await app.DATABASE.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    if not organization:
        organization = await app.DATABASE.create_organization(
            organization_name=SKYVERN_LOCAL_ORG,
            domain=SKYVERN_LOCAL_DOMAIN,
            max_steps_per_run=10,
            max_retries_per_step=3,
        )
        api_key = security.create_access_token(
            organization.organization_id,
            expires_delta=API_KEY_LIFETIME,
        )
        await app.DATABASE.create_org_auth_token(
            organization_id=organization.organization_id,
            token=api_key,
            token_type=OrganizationAuthTokenType.api,
        )
    return organization


async def setup_local_organization() -> str:
    organization = await get_or_create_local_organization()
    org_auth_token = await app.DATABASE.get_valid_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api.value,
    )
    return org_auth_token.token if org_auth_token else ""


def setup_mcp(*, local: bool = False) -> None:
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
        )

    if Confirm.ask("Would you like to set up MCP integration for Claude Desktop?", default=True):
        setup_claude(
            api_key=None,
            dry_run=False,
            yes=True,
            local=local,
            use_python_path=True,
            url=None,
        )

    if Confirm.ask("Would you like to set up MCP integration for Cursor?", default=True):
        setup_cursor(
            api_key=None,
            dry_run=False,
            yes=True,
            local=local,
            use_python_path=True,
            url=None,
        )

    if Confirm.ask("Would you like to set up MCP integration for Windsurf?", default=True):
        setup_windsurf(
            api_key=None,
            dry_run=False,
            yes=True,
            local=local,
            use_python_path=True,
            url=None,
        )

    console.print("\n🎉 [bold green]MCP server configuration completed.[/bold green]")
