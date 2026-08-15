from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import itertools
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from urllib.parse import urlsplit

import structlog
from aiohttp import WSMsgType, web

from skyvern.browser_extension.auth import build_challenge, compute_server_proof, verify_ext_proof
from skyvern.browser_extension.errors import (
    BrowserExtensionError,
    BrowserExtensionNotConnectedError,
    ExtensionRequestError,
)
from skyvern.browser_extension.protocol import (
    EXTENSION_ID,
    LEGACY_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    ParsedMessage,
    build_request,
    parse_extension_message,
)

LOG = structlog.get_logger(__name__)

_AUTH_TIMEOUT_SECONDS = 10.0
_PING_INTERVAL_SECONDS = 20.0
_INBOUND_TIMEOUT_SECONDS = 45.0
_MAX_WS_MESSAGE_BYTES = 256 * 1024 * 1024
_AUTH_CLOSE_CODE = 4403
_REPLACED_CLOSE_CODE = 4000
_PAIRING_NONCE_TTL_SECONDS = 120.0
_PAIR_BEGIN_MESSAGE = b"skyvern-pair-begin-v1"

_PAIR_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Pair Skyvern Agent</title>
  <style>
    :root {
      color-scheme: dark light;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #f4f4f5;
      background: #0b0c0f;
      font-synthesis: none;
      --accent: #6d6cf6;
      --accent-hover: #5f5eea;
      --surface: #141519;
      --surface-subtle: #1a1b20;
      --border: #2b2c33;
      --border-strong: #3a3b43;
      --text: #f4f4f5;
      --muted: #b2b3ba;
      --subtle: #85868f;
      --radius: .5rem;
    }

    * { box-sizing: border-box; }

    body {
      min-height: 100vh;
      margin: 0;
      display: grid;
      overflow-x: hidden;
      color: var(--text);
      background: #0b0c0f;
      place-items: center;
    }

    .page-shell {
      position: relative;
      width: 100%;
      padding: 2rem 1rem;
    }

    .pairing-card {
      width: min(38rem, 100%);
      margin: 0 auto;
      overflow: hidden;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--surface);
      box-shadow: 0 1rem 2.5rem rgb(0 0 0 / 28%);
    }

    .card-header {
      display: flex;
      align-items: center;
      gap: .75rem;
      padding: 1.5rem 1.75rem 0;
    }

    .brand-mark,
    .endpoint-mark {
      display: grid;
      flex: none;
      overflow: hidden;
      place-items: center;
    }

    .brand-mark {
      width: 2rem;
      height: 2rem;
      border-radius: var(--radius);
    }

    .brand-mark img,
    .endpoint-mark img {
      width: 100%;
      height: 100%;
      object-fit: cover;
    }

    .brand-copy { display: grid; }
    .brand-name { font-size: .9rem; font-weight: 650; letter-spacing: -.01em; }

    .card-body { padding: 2.5rem 1.75rem 1.75rem; }
    .intro { text-align: left; }

    h1 {
      margin: 0;
      font-size: clamp(2rem, 6vw, 2.35rem);
      font-weight: 680;
      line-height: 1.15;
      letter-spacing: -.035em;
    }

    .supporting {
      max-width: 32rem;
      margin: .85rem 0 0;
      color: var(--muted);
      font-size: .95rem;
      line-height: 1.55;
    }

    .connection {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 3.5rem minmax(0, 1fr);
      align-items: center;
      margin: 2.25rem 0;
    }

    .endpoint {
      display: grid;
      grid-template-columns: 2rem minmax(0, 1fr);
      grid-template-rows: auto auto;
      min-width: 0;
      align-items: center;
      column-gap: .7rem;
      row-gap: .2rem;
    }

    .endpoint-top {
      display: contents;
    }

    .endpoint-mark {
      grid-row: 1 / 3;
      width: 2rem;
      height: 2rem;
      border-radius: var(--radius);
      color: var(--muted);
    }

    .endpoint-mark svg { width: 1rem; height: 1rem; }
    .endpoint-title { overflow: hidden; font-size: .8rem; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
    .endpoint-meta { grid-column: 2; margin: 0; overflow: hidden; color: var(--subtle); font-size: .7rem; line-height: 1.4; text-overflow: ellipsis; white-space: nowrap; }
    #server-address { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }

    .connection-rail {
      position: relative;
      height: 1px;
      background: var(--border-strong);
    }

    .link-glyph {
      position: absolute;
      top: 50%;
      left: 50%;
      display: grid;
      width: 1.75rem;
      height: 1.75rem;
      color: var(--subtle);
      background: var(--surface);
      place-items: center;
      transform: translate(-50%, -50%);
    }

    .link-glyph svg { width: .95rem; height: .95rem; }

    .approval-controls { display: flex; flex-direction: column; align-items: flex-start; gap: .8rem; }

    .approve-button {
      display: inline-flex;
      width: auto;
      min-height: 2.75rem;
      align-items: center;
      justify-content: center;
      gap: .6rem;
      margin: 0;
      padding: .72rem 1.15rem;
      border: 1px solid var(--accent);
      border-radius: var(--radius);
      color: white;
      background: var(--accent);
      box-shadow: 0 .25rem .7rem rgb(0 0 0 / 24%);
      font: inherit;
      font-size: .9rem;
      font-weight: 650;
      cursor: pointer;
      transition: background 150ms ease, box-shadow 150ms ease;
    }

    .approve-button:hover:not(:disabled) {
      background: var(--accent-hover);
      box-shadow: 0 .3rem .85rem rgb(0 0 0 / 28%);
    }

    .approve-button:focus-visible,
    .copy-chip:focus-visible { outline: 2px solid var(--text); outline-offset: 3px; }
    .approve-button:disabled { cursor: wait; opacity: .78; }

    .spinner {
      display: none;
      width: 1rem;
      height: 1rem;
      border: 2px solid rgb(255 255 255 / 35%);
      border-top-color: white;
      border-radius: 50%;
      animation: spin 700ms linear infinite;
    }

    [data-state="approving"] .spinner { display: block; }
    .fine-print {
      margin: 0;
      color: var(--subtle);
      font-size: .7rem;
      line-height: 1.5;
      text-align: center;
    }

    .status-panel {
      display: grid;
      gap: .7rem;
      margin-top: .35rem;
      padding: 1rem 0 0;
      text-align: left;
    }

    .status-panel[hidden] { display: none; }
    .status-heading { display: flex; align-items: center; gap: .7rem; }

    .status-icon {
      display: grid;
      flex: none;
      width: 2rem;
      height: 2rem;
      color: var(--muted);
      place-items: center;
    }

    .status-icon svg { width: 1rem; height: 1rem; }
    .status-icon .success-icon { display: none; }
    .status-title { margin: 0; font-size: .9rem; font-weight: 650; }
    .status-message { margin: 0; color: var(--muted); font-size: .78rem; line-height: 1.55; }

    [data-state="success"] .status-icon .error-icon { display: none; }
    [data-state="success"] .status-icon .success-icon { display: block; }
    [data-state="success"] .approval-controls,
    [data-state="error"] .approval-controls { display: none; }

    @keyframes spin { to { transform: rotate(360deg); } }
    @media (max-width: 34rem) {
      .page-shell { padding: 1rem; }
      .card-header { padding: 1rem 1.1rem; }
      .card-body { padding: 1.7rem 1.2rem 1.35rem; }
      h1 { font-size: 1.9rem; }
      .connection { grid-template-columns: 1fr; gap: .85rem; }
      .connection-rail { width: 1px; height: 1.6rem; margin-left: 1rem; }
      .endpoint-title { overflow: visible; font-size: .78rem; line-height: 1.25; text-overflow: clip; white-space: normal; }
      .endpoint-meta { font-size: .67rem; }
      .link-glyph { width: 1.5rem; height: 1.5rem; }
      .supporting { font-size: .84rem; }
    }

    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; }
    }

    @media (prefers-color-scheme: light) {
      :root {
        color: #202124;
        background: #f5f5f7;
        --surface: #ffffff;
        --surface-subtle: #f5f5f7;
        --border: #dedfe3;
        --border-strong: #c9cbd1;
        --text: #202124;
        --muted: #5f6169;
        --subtle: #777982;
      }

      body { background: #f5f5f7; }
      .pairing-card { box-shadow: 0 1rem 2.5rem rgb(32 33 36 / 10%); }
    }
  </style>
</head>
<body data-state="idle">
  <div class="page-shell">
    <main class="pairing-card" aria-labelledby="pairing-title">
      <header class="card-header">
        <span class="brand-mark" aria-hidden="true">
          <img src="data:image/png;base64,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" alt="">
        </span>
        <span class="brand-copy">
          <span class="brand-name">Skyvern</span>
        </span>
      </header>

      <div class="card-body">
        <section class="intro">
          <h1 id="pairing-title">Pair Skyvern Agent</h1>
          <p class="supporting">Skyvern on this computer wants to drive Chrome through the Skyvern Agent extension.</p>
        </section>

        <div class="connection" aria-label="Skyvern MCP server connecting to Skyvern Agent extension">
          <div class="endpoint">
            <div class="endpoint-top">
              <span class="endpoint-mark" aria-hidden="true">
                <img src="data:image/png;base64,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" alt="">
              </span>
              <span class="endpoint-title">Skyvern MCP Server</span>
            </div>
            <p id="server-address" class="endpoint-meta">127.0.0.1</p>
          </div>

          <div class="connection-rail" aria-hidden="true">
            <span class="link-glyph">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <path d="M10 13a5 5 0 0 0 7.1.1l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1"/>
                <path d="M14 11a5 5 0 0 0-7.1-.1l-2 2A5 5 0 0 0 12 20l1.1-1.1"/>
              </svg>
            </span>
          </div>

          <div class="endpoint">
            <div class="endpoint-top">
              <span class="endpoint-mark" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                  <rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/>
                  <path d="M3.5 8.5h17"/>
                  <circle cx="6.5" cy="6.5" r=".5" fill="currentColor" stroke="none"/>
                  <circle cx="9" cy="6.5" r=".5" fill="currentColor" stroke="none"/>
                </svg>
              </span>
              <span class="endpoint-title">Chrome</span>
            </div>
            <p class="endpoint-meta">Skyvern Agent extension</p>
          </div>
        </div>

        <div id="approval-controls" class="approval-controls">
          <button id="approve" class="approve-button" type="button">
            <span class="spinner" aria-hidden="true"></span>
            <span id="approve-label">Approve connection</span>
          </button>
          <p class="fine-print">Secure pairing links expire after 2 minutes and can be used once.</p>
        </div>

        <section id="status" class="status-panel" role="status" aria-live="polite" hidden>
          <div class="status-heading">
            <span class="status-icon" aria-hidden="true">
              <svg class="error-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <path d="M12 8v5M12 17h.01"/>
                <circle cx="12" cy="12" r="9"/>
              </svg>
              <svg class="success-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
                <path d="m7 12.5 3.2 3.2L17.5 8.5"/>
              </svg>
            </span>
            <h2 id="status-title" class="status-title"></h2>
          </div>
          <p id="status-message" class="status-message"></p>
        </section>
      </div>
    </main>
  </div>
  <script>
    (() => {
      const nonce = location.hash.slice(1);
      const approvalControls = document.getElementById("approval-controls");
      const button = document.getElementById("approve");
      const buttonLabel = document.getElementById("approve-label");
      const serverAddress = document.getElementById("server-address");
      const status = document.getElementById("status");
      const statusTitle = document.getElementById("status-title");
      const statusMessage = document.getElementById("status-message");

      serverAddress.textContent = `127.0.0.1:${location.port}`;

      function showState(kind, title, message) {
        document.body.dataset.state = kind;
        status.hidden = false;
        statusTitle.textContent = title;
        statusMessage.textContent = message;
      }

      function showMissingRequest() {
        approvalControls.hidden = true;
        showState(
          "error",
          "Pairing request required",
          "Retry the browser session request in your MCP client, or run skyvern browser extension-pair again.",
        );
      }

      function showExpired() {
        showState(
          "error",
          "Pairing link expired",
          "Retry the browser session request in your MCP client, or run skyvern browser extension-pair again.",
        );
      }

      function showInstallHint() {
        showState(
          "error",
          "Skyvern Agent isn’t available",
          "Install or enable the Skyvern Agent Chrome extension, then retry the browser session request.",
        );
      }

      button.addEventListener("click", async () => {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        buttonLabel.textContent = "Approving…";
        document.body.dataset.state = "approving";
        if (!nonce) {
          showMissingRequest();
          return;
        }
        try {
          const response = await fetch("/pair/claim", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({v: 1, nonce}),
          });
          if (response.status === 403) {
            showExpired();
            return;
          }
          if (!response.ok) {
            throw new Error("claim failed");
          }
          const offer = await response.json();
          if (!globalThis.chrome?.runtime?.sendMessage) {
            showInstallHint();
            return;
          }
          chrome.runtime.sendMessage(
            "__EXTENSION_ID__",
            {type: "skyvern.pairingOffer", v: 1, port: offer.port, token: offer.token},
            (result) => {
              if (chrome.runtime.lastError) {
                showInstallHint();
              } else if (result?.ok === true && result?.pending === true) {
                showState(
                  "success",
                  "Approved — finish in the Skyvern Agent tab",
                  "Review the local server details in the confirmation tab that just opened.",
                );
              } else {
                showState(
                  "error",
                  "Pairing request wasn’t accepted",
                  "Retry the browser session request in your MCP client, or run skyvern browser extension-pair again.",
                );
              }
            },
          );
        } catch (_error) {
          showState(
            "error",
            "Pairing couldn’t be completed",
            "Check that the local MCP server is running, then retry the browser session request or run skyvern browser extension-pair again.",
          );
        }
      });

      if (!nonce) {
        showMissingRequest();
      }
    })();
  </script>
</body>
</html>
"""
_PAIR_PAGE = _PAIR_PAGE_TEMPLATE.replace("__EXTENSION_ID__", EXTENSION_ID)


class ExtensionRelayServer:
    def __init__(
        self,
        token: str,
        port: int,
        on_event: Callable[[str, dict], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]] | None = None,
        *,
        control_pairing_only: bool = False,
        on_pairing_complete: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._token = token
        self._port = port
        self._on_event = on_event
        self._on_disconnect = on_disconnect
        self._control_pairing_only = control_pairing_only
        self._on_pairing_complete = on_pairing_complete
        self._app = web.Application()
        self._app.router.add_get("/extension/v1", self._handle_websocket)
        self._app.router.add_get("/pair", self._handle_pair_page)
        self._app.router.add_post("/pair/begin", self._handle_pair_begin)
        self._app.router.add_post("/pair/claim", self._handle_pair_claim)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._websocket: web.WebSocketResponse | None = None
        self._connection_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._connected_event = asyncio.Event()
        self._request_ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._terminal_callbacks: dict[str, Callable[[], None]] = {}
        self._pending_empty = asyncio.Event()
        self._pending_empty.set()
        self._pairing_nonce: str | None = None
        self._pairing_nonce_created_at: float | None = None
        self.bound_port = port
        self.scoped_tabs: list[dict] = []
        self.extension_protocol_version: int | None = None
        self.extension_connection_generation = 0
        self._pending_reset_identity: tuple[str, int] | None = None

    def create_pairing_nonce(self) -> str:
        nonce = secrets.token_urlsafe(32)
        self._pairing_nonce = nonce
        self._pairing_nonce_created_at = time.monotonic()
        return nonce

    def get_or_create_pairing_nonce(self) -> str:
        nonce = self._pairing_nonce
        created_at = self._pairing_nonce_created_at
        if nonce is not None and created_at is not None and time.monotonic() - created_at < _PAIRING_NONCE_TTL_SECONDS:
            return nonce
        return self.create_pairing_nonce()

    def cancel_pairing_nonce(self) -> None:
        self._pairing_nonce = None
        self._pairing_nonce_created_at = None

    def _is_interactive_pairing_request(self, request: web.Request) -> bool:
        try:
            origin = urlsplit(request.headers.get("Origin", ""))
            origin_port = origin.port or (80 if origin.scheme == "http" else None)
        except ValueError:
            return False
        return (
            request.content_type == "application/json"
            and origin.scheme == "http"
            and origin.hostname == "127.0.0.1"
            and origin_port == self.bound_port
            and not origin.username
            and not origin.password
            and not origin.path
            and not origin.query
            and not origin.fragment
            and request.headers.get("Sec-Fetch-Mode") == "cors"
            and request.headers.get("Sec-Fetch-Site") == "same-origin"
        )

    async def start(self) -> None:
        if self._runner is not None:
            return

        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self._port)
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise

        server = site._server
        if server is None or not server.sockets:
            await runner.cleanup()
            raise BrowserExtensionError("Browser extension relay did not bind a loopback port")

        self.bound_port = int(server.sockets[0].getsockname()[1])
        self._runner = runner
        self._site = site

    async def stop(self) -> None:
        runner = self._runner
        if runner is None:
            return

        websocket = self._websocket
        if websocket is not None and not websocket.closed:
            await websocket.close(code=1001, message=b"relay stopped")
        await runner.cleanup()
        if websocket is not None:
            await self._handle_disconnect(websocket)
        self._runner = None
        self._site = None

    @property
    def connected(self) -> bool:
        return self._connected_event.is_set() and self._websocket is not None and not self._websocket.closed

    async def wait_connected(self, timeout: float) -> bool:
        if self.connected:
            return True
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout)
        except TimeoutError:
            return False
        return self.connected

    async def cycle_connection(self, timeout: float) -> bool:
        websocket = self._websocket
        if websocket is None:
            self.scoped_tabs = []
            return True
        try:
            if not websocket.closed:
                await asyncio.wait_for(
                    websocket.close(code=1001, message=b"broker client released"),
                    timeout,
                )
        except TimeoutError:
            return False
        await self._handle_disconnect(websocket)
        return True

    async def send_reset(self, epoch: str, generation: int) -> bool:
        websocket = self._websocket
        if (
            not isinstance(epoch, str)
            or not epoch
            or type(generation) is not int
            or generation < 0
            or self.extension_protocol_version != PROTOCOL_VERSION
            or websocket is None
            or websocket.closed
        ):
            return False
        reset_identity = (epoch, generation)
        self._pending_reset_identity = reset_identity
        try:
            await self._send_json(
                websocket,
                {"v": PROTOCOL_VERSION, "type": "extension.reset", "epoch": epoch, "generation": generation},
            )
        except (ConnectionError, RuntimeError):
            if self._pending_reset_identity == reset_identity:
                self._pending_reset_identity = None
            return False
        return True

    @property
    def pending_request_count(self) -> int:
        return len(self._pending)

    async def wait_pending_requests(self, timeout: float) -> bool:
        if not self._pending:
            return True
        try:
            await asyncio.wait_for(self._pending_empty.wait(), timeout)
        except TimeoutError:
            return False
        return not self._pending

    async def request(
        self,
        op: str,
        args: dict,
        timeout: float = 30.0,
        *,
        retain_until_terminal: bool = False,
        on_registered: Callable[[], None] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> dict:
        websocket = self._websocket
        if not self.connected or websocket is None:
            raise BrowserExtensionNotConnectedError("Skyvern browser extension is not connected")

        request_id = f"r-{next(self._request_ids)}"
        frame = build_request(
            request_id,
            op,
            args,
            protocol_version=self.extension_protocol_version or PROTOCOL_VERSION,
        )
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        if on_terminal is not None:
            self._terminal_callbacks[request_id] = on_terminal
        self._pending_empty.clear()
        if on_registered is not None:
            on_registered()
        try:
            await self._send_json(websocket, frame)
        except (ConnectionError, RuntimeError):
            pending = self._pop_pending(request_id)
            if pending is not None:
                pending.cancel()
            raise BrowserExtensionNotConnectedError("Skyvern browser extension is not connected") from None

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            if retain_until_terminal:
                future.add_done_callback(_consume_future_result)
            else:
                pending = self._pop_pending(request_id)
                if pending is not None:
                    pending.cancel()
            raise ExtensionRequestError("INTERNAL", f"extension request timed out: {op}") from None
        except asyncio.CancelledError:
            if retain_until_terminal:
                future.add_done_callback(_consume_future_result)
            else:
                pending = self._pop_pending(request_id)
                if pending is not None:
                    pending.cancel()
            raise

    async def _handle_pair_page(self, _request: web.Request) -> web.Response:
        LOG.info("browser_extension_pair_page_served")
        return web.Response(
            text=_PAIR_PAGE,
            content_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                    "img-src data:; connect-src 'self'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )

    async def _handle_pair_begin(self, request: web.Request) -> web.Response:
        if self._control_pairing_only:
            return web.json_response(
                {"error": "broker_control_required"}, status=404, headers={"Cache-Control": "no-store"}
            )
        payload = await _read_json_object(request)
        proof = payload.get("proof") if payload is not None else None
        expected = hmac.new(self._token.encode("utf-8"), _PAIR_BEGIN_MESSAGE, hashlib.sha256).hexdigest()
        supplied = proof if isinstance(proof, str) else ""
        valid = hmac.compare_digest(expected, supplied)
        if payload is None or type(payload.get("v")) is not int or payload["v"] != LEGACY_PROTOCOL_VERSION or not valid:
            return web.json_response({"error": "invalid_proof"}, status=403, headers={"Cache-Control": "no-store"})
        return web.json_response(
            {"v": LEGACY_PROTOCOL_VERSION, "nonce": self.create_pairing_nonce()},
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_pair_claim(self, request: web.Request) -> web.Response:
        if not self._is_interactive_pairing_request(request):
            LOG.info("browser_extension_pair_claim", outcome="invalid_source")
            return web.json_response({"error": "invalid_source"}, status=403, headers={"Cache-Control": "no-store"})

        active_nonce = self._pairing_nonce
        created_at = self._pairing_nonce_created_at
        self._pairing_nonce = None
        self._pairing_nonce_created_at = None

        payload = await _read_json_object(request)
        supplied_nonce = payload.get("nonce") if payload is not None else None
        valid_payload = (
            payload is not None
            and type(payload.get("v")) is int
            and payload["v"] == LEGACY_PROTOCOL_VERSION
            and isinstance(supplied_nonce, str)
        )
        nonce_matches = secrets.compare_digest(
            active_nonce or ("0" * 43),
            supplied_nonce if isinstance(supplied_nonce, str) else "",
        )
        expired = created_at is None or time.monotonic() - created_at >= _PAIRING_NONCE_TTL_SECONDS
        if not valid_payload:
            LOG.info("browser_extension_pair_claim", outcome="bad_payload")
            return web.json_response({"error": "invalid_nonce"}, status=403, headers={"Cache-Control": "no-store"})
        if not nonce_matches or expired:
            LOG.info("browser_extension_pair_claim", outcome="expired_or_unknown_nonce")
            return web.json_response({"error": "invalid_nonce"}, status=403, headers={"Cache-Control": "no-store"})
        LOG.info("browser_extension_pair_claim", outcome="ok")
        await self._call_on_pairing_complete()
        return web.json_response(
            {"v": LEGACY_PROTOCOL_VERSION, "port": self.bound_port, "token": self._token},
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse(max_msg_size=_MAX_WS_MESSAGE_BYTES)
        await websocket.prepare(request)
        LOG.info("browser_extension_websocket_opened", remote_address=request.remote)

        try:
            origin = request.headers.get("Origin")
            if origin is not None and not _is_extension_origin(origin):
                await self._reject_authentication(websocket, "invalid_origin")
                return websocket

            server_nonce, challenge = build_challenge()
            await websocket.send_str(challenge)
            try:
                proof_frame = await websocket.receive(timeout=_AUTH_TIMEOUT_SECONDS)
            except TimeoutError:
                await self._reject_authentication(websocket, "timeout")
                return websocket
            if proof_frame.type is not WSMsgType.TEXT:
                await self._reject_authentication(websocket, "bad_payload")
                return websocket
            try:
                proof = parse_extension_message(proof_frame.data)
            except BrowserExtensionError:
                await self._reject_authentication(websocket, _auth_parse_failure_reason(proof_frame.data))
                return websocket
            if proof.kind != "auth.proof" or proof.client_nonce is None or proof.proof is None:
                await self._reject_authentication(websocket, "bad_payload")
                return websocket
            if not _is_valid_client_nonce(proof.client_nonce):
                await self._reject_authentication(websocket, "bad_nonce")
                return websocket
            if not verify_ext_proof(self._token, server_nonce, proof.client_nonce, proof.proof):
                await self._reject_authentication(websocket, "bad_proof")
                return websocket

            await self._send_json(
                websocket,
                {
                    "v": proof.protocol_version,
                    "type": "auth.ok",
                    "serverProof": compute_server_proof(self._token, proof.client_nonce, server_nonce),
                },
            )
            LOG.info("browser_extension_auth_succeeded")
            await self._activate_connection(websocket, proof.protocol_version)

            loop = asyncio.get_running_loop()
            last_inbound = loop.time()
            keepalive_task = asyncio.create_task(self._run_keepalive(websocket, lambda: last_inbound))
            try:
                async for message in websocket:
                    last_inbound = loop.time()
                    if self._websocket is not websocket:
                        break
                    if message.type is WSMsgType.TEXT:
                        await self._handle_text_frame(websocket, message.data)
                    elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                        break
                    else:
                        LOG.warning("browser extension sent a non-text frame")
            finally:
                keepalive_task.cancel()
                with suppress(asyncio.CancelledError):
                    await keepalive_task
                await self._handle_disconnect(websocket)
            return websocket
        finally:
            LOG.info("browser_extension_websocket_closed", close_code=websocket.close_code)

    async def _reject_authentication(self, websocket: web.WebSocketResponse, reason: str) -> None:
        LOG.info("browser_extension_auth_failed", reason=reason)
        await websocket.close(code=_AUTH_CLOSE_CODE, message=b"authentication failed")

    async def _activate_connection(self, websocket: web.WebSocketResponse, protocol_version: int) -> None:
        async with self._connection_lock:
            previous = self._websocket
            self._websocket = websocket
            self.extension_protocol_version = protocol_version
            self.extension_connection_generation += 1
            self._pending_reset_identity = None
            self._connected_event.clear()

        if previous is not None and previous is not websocket:
            LOG.info("browser_extension_websocket_replaced")
            self._fail_pending_requests()
            self.scoped_tabs = []
            await self._call_on_disconnect()
            if not previous.closed:
                await previous.close(code=_REPLACED_CLOSE_CODE, message=b"replaced")

    async def _handle_text_frame(self, websocket: web.WebSocketResponse, raw: str) -> None:
        try:
            message = parse_extension_message(raw)
        except BrowserExtensionError:
            LOG.warning("browser extension sent an invalid protocol frame")
            return

        if self.extension_protocol_version is not None and message.protocol_version != self.extension_protocol_version:
            LOG.warning("browser extension changed protocol version within one connection")
            return

        if message.kind == "response":
            self._handle_response(message)
        elif message.kind == "event":
            await self._handle_event(message)
            if message.event == "extension.hello":
                await self._mark_hello_processed(websocket)
        elif message.kind == "extension.reset_ack":
            reset_identity = self._pending_reset_identity
            if (
                reset_identity is not None
                and message.reset_epoch is not None
                and message.generation is not None
                and (message.reset_epoch, message.generation) == reset_identity
                and message.ok is True
            ):
                self._pending_reset_identity = None
                self._fail_pending_requests()
                self.scoped_tabs = []
            await self._on_event(
                "extension.reset_ack",
                {
                    "epoch": message.reset_epoch,
                    "generation": message.generation,
                    "ok": message.ok,
                    "failedTabCount": message.failed_tab_count,
                },
            )
        elif message.kind == "ping":
            await self._send_json(websocket, {"v": message.protocol_version, "type": "pong"})

    async def _send_json(self, websocket: web.WebSocketResponse, frame: dict) -> None:
        async with self._send_lock:
            await websocket.send_json(frame)

    async def _mark_hello_processed(self, websocket: web.WebSocketResponse) -> None:
        async with self._connection_lock:
            current = self._websocket
            if current is websocket and current is not None and not current.closed:
                self._connected_event.set()

    def _handle_response(self, message: ParsedMessage) -> None:
        if message.request_id is None:
            return
        future = self._pop_pending(message.request_id)
        if future is None or future.done():
            return
        if message.ok:
            future.set_result(message.result or {})
            return
        if message.error_code is None or message.error_message is None:
            future.set_exception(ExtensionRequestError("INTERNAL", "extension returned an invalid error"))
            return
        future.set_exception(ExtensionRequestError(message.error_code, message.error_message))

    async def _handle_event(self, message: ParsedMessage) -> None:
        if message.event is None or message.params is None:
            return
        self._update_scoped_tabs(message.event, message.params)
        try:
            await self._on_event(message.event, message.params)
        except Exception:
            LOG.exception("browser extension event callback failed", event=message.event)

    def _update_scoped_tabs(self, event: str, params: dict) -> None:
        if event == "extension.hello":
            tabs = params.get("scopedTabs")
            if not isinstance(tabs, list):
                self.scoped_tabs = []
                return
            self.scoped_tabs = [snapshot for tab in tabs if (snapshot := _tab_snapshot(tab)) is not None]
            return
        if event in {"scope.tabAdded", "tabs.created"}:
            snapshot = _tab_snapshot(params)
            if snapshot is None:
                return
            self.scoped_tabs = [tab for tab in self.scoped_tabs if tab.get("tabId") != snapshot["tabId"]]
            self.scoped_tabs.append(snapshot)
            return
        if event == "scope.tabRemoved":
            tab_id = params.get("tabId")
            if type(tab_id) is int:
                self.scoped_tabs = [tab for tab in self.scoped_tabs if tab.get("tabId") != tab_id]

    async def _run_keepalive(
        self,
        websocket: web.WebSocketResponse,
        last_inbound: Callable[[], float],
    ) -> None:
        loop = asyncio.get_running_loop()
        next_ping = loop.time() + _PING_INTERVAL_SECONDS
        while not websocket.closed:
            now = loop.time()
            silence = now - last_inbound()
            if silence >= _INBOUND_TIMEOUT_SECONDS:
                await websocket.close(code=1001, message=b"inbound timeout")
                return
            if now >= next_ping:
                try:
                    await self._send_json(
                        websocket,
                        {"v": self.extension_protocol_version or PROTOCOL_VERSION, "type": "ping"},
                    )
                except (ConnectionError, RuntimeError):
                    return
                next_ping = now + _PING_INTERVAL_SECONDS
            delay = min(next_ping - now, _INBOUND_TIMEOUT_SECONDS - silence)
            await asyncio.sleep(max(delay, 0.01))

    async def _handle_disconnect(self, websocket: web.WebSocketResponse) -> None:
        async with self._connection_lock:
            if self._websocket is not websocket:
                return
            self._websocket = None
            self.extension_protocol_version = None
            self._pending_reset_identity = None
            self._connected_event.clear()

        self._fail_pending_requests()
        self.scoped_tabs = []
        await self._call_on_disconnect()

    async def _call_on_disconnect(self) -> None:
        if self._on_disconnect is not None:
            try:
                await self._on_disconnect()
            except Exception:
                LOG.exception("browser extension disconnect callback failed")

    async def _call_on_pairing_complete(self) -> None:
        if self._on_pairing_complete is not None:
            try:
                await self._on_pairing_complete()
            except Exception:
                LOG.exception("browser extension pairing-complete callback failed")

    def _pop_pending(self, request_id: str) -> asyncio.Future[dict] | None:
        future = self._pending.pop(request_id, None)
        terminal_callback = self._terminal_callbacks.pop(request_id, None)
        if not self._pending:
            self._pending_empty.set()
        if terminal_callback is not None:
            terminal_callback()
        return future

    def _fail_pending_requests(self) -> None:
        pending = list(self._pending.values())
        terminal_callbacks = list(self._terminal_callbacks.values())
        self._pending.clear()
        self._terminal_callbacks.clear()
        self._pending_empty.set()
        for terminal_callback in terminal_callbacks:
            terminal_callback()
        for future in pending:
            if not future.done():
                future.set_exception(BrowserExtensionNotConnectedError("Skyvern browser extension is not connected"))


def _consume_future_result(future: asyncio.Future[dict]) -> None:
    if not future.cancelled():
        with suppress(Exception):
            future.exception()


def _is_extension_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme == "chrome-extension" and bool(parsed.netloc)


async def _read_json_object(request: web.Request) -> dict | None:
    try:
        payload = await request.json(loads=json.loads)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _auth_parse_failure_reason(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "bad_payload"
    if isinstance(payload, dict) and type(payload.get("v")) is int and payload["v"] not in SUPPORTED_PROTOCOL_VERSIONS:
        return "protocol_mismatch"
    return "bad_payload"


def _is_valid_client_nonce(client_nonce: str) -> bool:
    if not client_nonce or "=" in client_nonce:
        return False
    padding = "=" * (-len(client_nonce) % 4)
    try:
        decoded = base64.b64decode(client_nonce + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return False
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    return len(decoded) == 32 and canonical == client_nonce


def _tab_snapshot(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    tab_id = value.get("tabId")
    if type(tab_id) is not int:
        return None
    url = value.get("url")
    title = value.get("title")
    return {
        "tabId": tab_id,
        "url": url if isinstance(url, str) else "",
        "title": title if isinstance(title, str) else "",
    }
