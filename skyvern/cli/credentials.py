"""Credential management CLI commands.

Provides `skyvern credentials add/list/get/delete` for managing stored
credentials. Passwords and secrets are collected via getpass (stdin) so they
never appear in shell history or LLM conversation logs.
"""

from __future__ import annotations

import os

import typer
from dotenv import load_dotenv
from rich.table import Table

from skyvern.client import Skyvern
from skyvern.client.types.non_empty_credit_card_credential import NonEmptyCreditCardCredential
from skyvern.client.types.non_empty_password_credential import NonEmptyPasswordCredential
from skyvern.client.types.secret_credential import SecretCredential
from skyvern.config import settings
from skyvern.utils.env_paths import resolve_backend_env_path

from .console import console

credentials_app = typer.Typer(
    help="Manage stored credentials for secure login. Use `credential` commands for MCP-parity list/get/delete."
)


@credentials_app.callback()
def credentials_callback(
    ctx: typer.Context,
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="Skyvern API key",
        envvar="SKYVERN_API_KEY",
    ),
) -> None:
    """Store API key in Typer context."""
    ctx.obj = {"api_key": api_key}


def _get_client(api_key: str | None = None) -> Skyvern:
    """Instantiate a Skyvern SDK client using environment variables."""
    load_dotenv(resolve_backend_env_path())
    key = api_key or os.getenv("SKYVERN_API_KEY") or settings.SKYVERN_API_KEY
    return Skyvern(base_url=settings.SKYVERN_BASE_URL, api_key=key)


@credentials_app.command("add")
def add_credential(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", "-n", help="Human-readable credential name"),
    credential_type: str = typer.Option(
        "password",
        "--type",
        "-t",
        help="Credential type: password, credit_card, or secret",
    ),
    username: str | None = typer.Option(None, "--username", "-u", help="Username (for password type)"),
) -> None:
    """Create a credential with secrets entered securely via stdin."""
    valid_types = ("password", "credit_card", "secret")
    if credential_type not in valid_types:
        console.print(f"[red]Invalid credential type: {credential_type}. Use one of: {', '.join(valid_types)}[/red]")
        raise typer.Exit(code=1)

    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)

    if credential_type == "password":
        if not username:
            username = typer.prompt("Username")
        password = typer.prompt("Password", hide_input=True)
        if not password:
            console.print("[red]Password cannot be empty.[/red]")
            raise typer.Exit(code=1)
        totp = typer.prompt("TOTP secret (leave blank to skip)", default="", hide_input=True)
        credential = NonEmptyPasswordCredential(
            username=username,
            password=password,
            totp=totp if totp else None,
        )

    elif credential_type == "credit_card":
        card_number = typer.prompt("Card number", hide_input=True)
        if not card_number:
            console.print("[red]Card number cannot be empty.[/red]")
            raise typer.Exit(code=1)
        cvv = typer.prompt("CVV", hide_input=True)
        if not cvv:
            console.print("[red]CVV cannot be empty.[/red]")
            raise typer.Exit(code=1)
        exp_month = typer.prompt("Expiration month (MM)")
        exp_year = typer.prompt("Expiration year (YYYY)")
        brand = typer.prompt("Card brand (e.g. visa, mastercard)")
        holder_name = typer.prompt("Cardholder name")
        credential = NonEmptyCreditCardCredential(
            card_number=card_number,
            card_cvv=cvv,
            card_exp_month=exp_month,
            card_exp_year=exp_year,
            card_brand=brand,
            card_holder_name=holder_name,
        )

    else:
        secret_value = typer.prompt("Secret value", hide_input=True)
        if not secret_value:
            console.print("[red]Secret value cannot be empty.[/red]")
            raise typer.Exit(code=1)
        secret_label = typer.prompt("Secret label (leave blank to skip)", default="")
        credential = SecretCredential(
            secret_value=secret_value,
            secret_label=secret_label if secret_label else None,
        )

    try:
        result = client.create_credential(
            name=name,
            credential_type=credential_type,
            credential=credential,
        )
    except Exception as e:
        console.print(f"[red]Failed to create credential: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]Created credential:[/green] {result.credential_id}")


@credentials_app.command("list")
def list_credentials(
    ctx: typer.Context,
    page: int = typer.Option(1, "--page", help="Page number"),
    page_size: int = typer.Option(10, "--page-size", help="Results per page"),
) -> None:
    """List stored credentials (metadata only, never passwords)."""
    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)

    try:
        credentials = client.get_credentials(page=page, page_size=page_size)
    except Exception as e:
        console.print(f"[red]Failed to list credentials: {e}[/red]")
        raise typer.Exit(code=1)

    if not credentials:
        console.print("No credentials found.")
        return

    table = Table(title="Credentials")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Type")
    table.add_column("Details")

    for cred in credentials:
        details = ""
        c = cred.credential
        if hasattr(c, "username"):
            details = f"username={c.username}"
        elif hasattr(c, "last_four"):
            details = f"****{c.last_four} ({c.brand})"
        elif hasattr(c, "secret_label") and c.secret_label:
            details = f"label={c.secret_label}"
        table.add_row(cred.credential_id, cred.name, str(cred.credential_type), details)

    console.print(table)


@credentials_app.command("get")
def get_credential(
    ctx: typer.Context,
    credential_id: str = typer.Argument(..., help="Credential ID (starts with cred_)"),
) -> None:
    """Show metadata for a single credential."""
    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)

    try:
        cred = client.get_credential(credential_id)
    except Exception as e:
        console.print(f"[red]Failed to get credential: {e}[/red]")
        raise typer.Exit(code=1)

    table = Table(title=f"Credential: {cred.name}")
    table.add_column("Field", style="cyan")
    table.add_column("Value")

    table.add_row("ID", cred.credential_id)
    table.add_row("Name", cred.name)
    table.add_row("Type", str(cred.credential_type))

    c = cred.credential
    if hasattr(c, "username"):
        table.add_row("Username", c.username)
        if hasattr(c, "totp_type") and c.totp_type:
            table.add_row("TOTP Type", str(c.totp_type))
    elif hasattr(c, "last_four"):
        table.add_row("Card Last Four", c.last_four)
        table.add_row("Card Brand", c.brand)
    elif hasattr(c, "secret_label") and c.secret_label:
        table.add_row("Secret Label", c.secret_label)

    console.print(table)


@credentials_app.command("delete")
def delete_credential(
    ctx: typer.Context,
    credential_id: str = typer.Argument(..., help="Credential ID to delete (starts with cred_)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Permanently delete a stored credential."""
    if not yes:
        confirm = typer.confirm(f"Delete credential {credential_id}?")
        if not confirm:
            console.print("Aborted.")
            raise typer.Exit()

    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)

    try:
        client.delete_credential(credential_id)
    except Exception as e:
        console.print(f"[red]Failed to delete credential: {e}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]Deleted credential:[/green] {credential_id}")
