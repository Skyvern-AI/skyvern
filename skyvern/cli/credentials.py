"""Credential management CLI commands.

Provides `skyvern credentials add/list/get/delete` for managing stored
credentials. Passwords and secrets are collected via getpass (stdin) so they
never appear in shell history or LLM conversation logs.

Non-interactive mode: set SKYVERN_NON_INTERACTIVE=1 or CI=true and pass
all secret values via flags (--password, --totp, etc.) to avoid prompts.
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

from .commands._output import output, output_error
from .commands._tty import is_interactive, require_interactive_or_flag
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
    ctx.ensure_object(dict)
    ctx.obj["api_key"] = api_key


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
    username: str | None = typer.Option(
        None, "--username", "-u", envvar="SKYVERN_CRED_USERNAME", help="Username (for password type)"
    ),
    password: str | None = typer.Option(
        None,
        "--password",
        envvar="SKYVERN_CRED_PASSWORD",
        help="Password (prefer env var SKYVERN_CRED_PASSWORD over flag — flags are visible in ps)",
    ),
    totp: str | None = typer.Option(
        None, "--totp", envvar="SKYVERN_CRED_TOTP", help="TOTP secret (prefer env var SKYVERN_CRED_TOTP)"
    ),
    card_number: str | None = typer.Option(
        None,
        "--card-number",
        envvar="SKYVERN_CRED_CARD_NUMBER",
        help="Card number (prefer env var SKYVERN_CRED_CARD_NUMBER)",
    ),
    cvv: str | None = typer.Option(
        None, "--cvv", envvar="SKYVERN_CRED_CVV", help="Card CVV (prefer env var SKYVERN_CRED_CVV)"
    ),
    exp_month: str | None = typer.Option(
        None, "--exp-month", envvar="SKYVERN_CRED_EXP_MONTH", help="Card expiration month MM"
    ),
    exp_year: str | None = typer.Option(
        None, "--exp-year", envvar="SKYVERN_CRED_EXP_YEAR", help="Card expiration year YYYY"
    ),
    card_brand: str | None = typer.Option(
        None, "--card-brand", envvar="SKYVERN_CRED_CARD_BRAND", help="Card brand (visa, mastercard, etc.)"
    ),
    holder_name: str | None = typer.Option(
        None, "--holder-name", envvar="SKYVERN_CRED_HOLDER_NAME", help="Cardholder name"
    ),
    secret_value: str | None = typer.Option(
        None,
        "--secret-value",
        envvar="SKYVERN_CRED_SECRET_VALUE",
        help="Secret value (prefer env var SKYVERN_CRED_SECRET_VALUE)",
    ),
    secret_label: str | None = typer.Option(
        None, "--secret-label", envvar="SKYVERN_CRED_SECRET_LABEL", help="Secret label"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Create a credential securely.

    Secrets can be provided via env vars (preferred) or interactive prompts.
    CLI flags for secrets (--password, --cvv, etc.) are visible in ps/proc —
    use env vars instead: export SKYVERN_CRED_PASSWORD=... before running.

    Examples:
      export SKYVERN_CRED_PASSWORD=s3cret
      skyvern credentials add --name MyLogin --type password --username user@example.com --json
      skyvern credentials add --name MyLogin --type password --username user@example.com
    """
    valid_types = ("password", "credit_card", "secret")
    if credential_type not in valid_types:
        output_error(
            f"Invalid credential type: {credential_type}. Use one of: {', '.join(valid_types)}",
            action="credentials.add",
            json_mode=json_output,
        )

    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)

    if credential_type == "password":
        username = require_interactive_or_flag(
            username,
            flag_name="username",
            message="Username required.",
            action="credentials.add",
            json_mode=json_output,
        )
        if username is None:
            username = typer.prompt("Username")
        password = require_interactive_or_flag(
            password,
            flag_name="password",
            message="Password required.",
            action="credentials.add",
            json_mode=json_output,
        )
        if password is None:
            password = typer.prompt("Password", hide_input=True)
        if not password:
            output_error("Password cannot be empty.", action="credentials.add", json_mode=json_output)
        if totp is None and is_interactive():
            totp = typer.prompt("TOTP secret (leave blank to skip)", default="", hide_input=True)
        credential = NonEmptyPasswordCredential(
            username=username,
            password=password,
            totp=totp if totp else None,
        )

    elif credential_type == "credit_card":
        card_number = require_interactive_or_flag(
            card_number,
            flag_name="card-number",
            message="Card number required.",
            action="credentials.add",
            json_mode=json_output,
        )
        if card_number is None:
            card_number = typer.prompt("Card number", hide_input=True)
        if not card_number:
            output_error("Card number cannot be empty.", action="credentials.add", json_mode=json_output)
        cvv = require_interactive_or_flag(
            cvv,
            flag_name="cvv",
            message="CVV required.",
            action="credentials.add",
            json_mode=json_output,
        )
        if cvv is None:
            cvv = typer.prompt("CVV", hide_input=True)
        if not cvv:
            output_error("CVV cannot be empty.", action="credentials.add", json_mode=json_output)
        exp_month = require_interactive_or_flag(
            exp_month,
            flag_name="exp-month",
            message="Expiration month required.",
            action="credentials.add",
            json_mode=json_output,
        )
        if exp_month is None:
            exp_month = typer.prompt("Expiration month (MM)")
        exp_year = require_interactive_or_flag(
            exp_year,
            flag_name="exp-year",
            message="Expiration year required.",
            action="credentials.add",
            json_mode=json_output,
        )
        if exp_year is None:
            exp_year = typer.prompt("Expiration year (YYYY)")
        card_brand = require_interactive_or_flag(
            card_brand,
            flag_name="card-brand",
            message="Card brand required.",
            action="credentials.add",
            json_mode=json_output,
        )
        if card_brand is None:
            card_brand = typer.prompt("Card brand (e.g. visa, mastercard)")
        holder_name = require_interactive_or_flag(
            holder_name,
            flag_name="holder-name",
            message="Cardholder name required.",
            action="credentials.add",
            json_mode=json_output,
        )
        if holder_name is None:
            holder_name = typer.prompt("Cardholder name")
        credential = NonEmptyCreditCardCredential(
            card_number=card_number,
            card_cvv=cvv,
            card_exp_month=exp_month,
            card_exp_year=exp_year,
            card_brand=card_brand,
            card_holder_name=holder_name,
        )

    else:
        secret_value = require_interactive_or_flag(
            secret_value,
            flag_name="secret-value",
            message="Secret value required.",
            action="credentials.add",
            json_mode=json_output,
        )
        if secret_value is None:
            secret_value = typer.prompt("Secret value", hide_input=True)
        if not secret_value:
            output_error("Secret value cannot be empty.", action="credentials.add", json_mode=json_output)
        if secret_label is None and is_interactive():
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
        output_error(f"Failed to create credential: {e}", action="credentials.add", json_mode=json_output)

    if json_output:
        output({"credential_id": result.credential_id, "name": name}, action="credentials.add", json_mode=True)
    else:
        console.print(f"[green]Created credential:[/green] {result.credential_id}")


@credentials_app.command("list")
def list_credentials(
    ctx: typer.Context,
    page: int = typer.Option(1, "--page", help="Page number"),
    page_size: int = typer.Option(10, "--page-size", help="Results per page"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List stored credentials (metadata only, never passwords)."""
    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)

    try:
        credentials = client.get_credentials(page=page, page_size=page_size)
    except Exception as e:
        output_error(f"Failed to list credentials: {e}", action="credentials.list", json_mode=json_output)

    if json_output:
        data = [
            {
                "credential_id": c.credential_id,
                "name": c.name,
                "credential_type": str(c.credential_type),
            }
            for c in (credentials or [])
        ]
        output(data, action="credentials.list", json_mode=True)
        return

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
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show metadata for a single credential."""
    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)

    try:
        cred = client.get_credential(credential_id)
    except Exception as e:
        output_error(f"Failed to get credential: {e}", action="credentials.get", json_mode=json_output)

    if json_output:
        data = {
            "credential_id": cred.credential_id,
            "name": cred.name,
            "credential_type": str(cred.credential_type),
        }
        output(data, action="credentials.get", json_mode=True)
        return

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
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Permanently delete a stored credential.

    Examples:
      skyvern credentials delete cred_abc123 --yes
      skyvern credentials delete cred_abc123 --yes --json
    """
    if not yes:
        if not is_interactive():
            output_error(
                "Confirmation required for delete. Running in non-interactive mode.",
                hint="skyvern credentials delete <id> --yes",
                action="credentials.delete",
                json_mode=json_output,
            )
        confirm = typer.confirm(f"Delete credential {credential_id}?")
        if not confirm:
            console.print("Aborted.")
            raise typer.Exit()

    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)

    try:
        client.delete_credential(credential_id)
    except Exception as e:
        output_error(f"Failed to delete credential: {e}", action="credentials.delete", json_mode=json_output)

    if json_output:
        output({"credential_id": credential_id, "deleted": True}, action="credentials.delete", json_mode=True)
    else:
        console.print(f"[green]Deleted credential:[/green] {credential_id}")
