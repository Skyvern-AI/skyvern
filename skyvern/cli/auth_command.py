from __future__ import annotations

import http.server
import secrets
import socket
import threading
import urllib.parse
import webbrowser

import typer

from .console import console

_DEFAULT_FRONTEND_URL = "https://app.skyvern.com"
_CALLBACK_TIMEOUT = 300

_SUCCESS_HTML = """\
<!DOCTYPE html>
<html>
<body style="font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #0a0a0a; color: #fafafa;">
<div style="text-align: center;">
<h1>Signup Successful</h1>
<p>You can close this tab and return to your terminal.</p>
</div>
</body>
</html>"""


def _find_free_port() -> socket.socket:
    """Bind to a free port and return the socket (kept open to prevent TOCTOU race)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    return s


def _derive_api_base_url(frontend_url: str) -> str:
    """Derive the API base URL from the frontend URL.

    app.skyvern.com -> https://api.skyvern.com
    localhost:8080  -> http://localhost:8000
    """
    parsed = urllib.parse.urlparse(frontend_url)
    hostname = parsed.hostname or ""
    if hostname in ("localhost", "127.0.0.1"):
        return "http://localhost:8000"
    if hostname.startswith("app."):
        new_host = "api." + hostname[4:]
        if parsed.port:
            new_host = f"{new_host}:{parsed.port}"
        return urllib.parse.urlunparse(parsed._replace(netloc=new_host))
    console.print(
        f"[yellow]Could not derive API base URL from '{frontend_url}'. "
        f"You may need to set SKYVERN_BASE_URL manually in .env.[/yellow]"
    )
    return frontend_url


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that captures the auth callback via form POST.

    The frontend submits a hidden HTML form (browser navigation, not fetch),
    which avoids CORS / Private Network Access issues entirely.
    State is stored on the server instance (self.server).
    """

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        data = urllib.parse.parse_qs(body)

        # Validate state nonce to prevent CSRF
        expected_state = getattr(self.server, "expected_state", None)
        state_values = data.get("state", [])
        if not state_values or state_values[0] != expected_state:
            self.send_error(403, "Invalid state parameter")
            return

        api_key_values = data.get("api_key", [])
        if not api_key_values or not api_key_values[0]:
            self.send_error(400, "Missing api_key")
            return

        org_id_values = data.get("organization_id", [])
        email_values = data.get("email", [])

        self.server.auth_result = {  # type: ignore[attr-defined]
            "api_key": api_key_values[0],
            "organization_id": org_id_values[0] if org_id_values else None,
            "email": email_values[0] if email_values else None,
        }

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_SUCCESS_HTML.encode())

        self.server.received_event.set()  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress default HTTP server logs


def run_signup(
    base_url: str = _DEFAULT_FRONTEND_URL,
    timeout: int = _CALLBACK_TIMEOUT,
) -> None:
    """Core signup logic. Called by both the Typer command and init_command."""
    from .llm_setup import update_or_add_env_var

    bound_socket = _find_free_port()
    port = bound_socket.getsockname()[1]
    state = secrets.token_urlsafe(32)

    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler, bind_and_activate=False)
    server.socket = bound_socket
    server.server_activate()
    server.auth_result = {"api_key": None, "organization_id": None, "email": None}  # type: ignore[attr-defined]
    server.received_event = threading.Event()  # type: ignore[attr-defined]
    server.expected_state = state  # type: ignore[attr-defined]

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        auth_url = f"{base_url.rstrip('/')}/cli-auth?port={port}&state={state}"
        console.print("Opening browser for Skyvern signup...")
        console.print(f"If the browser doesn't open, visit: [link]{auth_url}[/link]")
        webbrowser.open(auth_url)

        if not server.received_event.wait(timeout=timeout):  # type: ignore[attr-defined]
            console.print("[red]Signup timed out. Please try again.[/red]")
            raise typer.Exit(code=1)

        result = server.auth_result  # type: ignore[attr-defined]
        api_key = result["api_key"]
        organization_id = result["organization_id"]
        email = result["email"]

        if not api_key:
            console.print("[red]Failed to receive API key. Please try again.[/red]")
            raise typer.Exit(code=1)
    finally:
        server.shutdown()

    api_base_url = _derive_api_base_url(base_url)

    update_or_add_env_var("SKYVERN_API_KEY", api_key)
    update_or_add_env_var("SKYVERN_BASE_URL", api_base_url)

    console.print("\n[bold green]Signup successful![/bold green]")
    if email:
        console.print(f"Email: {email}")
    if organization_id:
        console.print(f"Organization: {organization_id}")
    console.print("API key saved to .env")
    console.print(f"Base URL: {api_base_url}")


def signup(
    base_url: str = typer.Option(
        _DEFAULT_FRONTEND_URL,
        "--base-url",
        help="Frontend URL (e.g. http://localhost:8080 for local dev)",
    ),
    timeout: int = typer.Option(
        _CALLBACK_TIMEOUT,
        "--timeout",
        help="Timeout in seconds waiting for browser signup",
    ),
) -> None:
    """Sign up for Skyvern Cloud and save your API key."""
    run_signup(base_url=base_url, timeout=timeout)
