"""End-to-end semantic validation for the code-block authentication fix.

This does NOT require a Skyvern server, a database, or an LLM key. It stands up
a tiny auth-gated website and reproduces, with real Playwright BrowserContexts,
the exact behavior the Skyvern fix governs:

  * "agent block"  -> logs in inside a BrowserContext (cookie stored there)
  * "code block (BROKEN)"  -> a *fresh* BrowserContext does page.goto(/dashboard)
                              and lands on the sign-in page (the reported bug)
  * "code block (FIXED)"   -> *reusing the agent's* BrowserContext does
                              page.goto(/dashboard) + page.evaluate(...) and is
                              authenticated

The Skyvern fix makes the Code Block reuse the agent's live BrowserContext
(``BROWSER_MANAGER.get_for_workflow_run``) instead of acquiring a separate one,
which is precisely the difference between the two cases below.

Run:  uv run python scripts/validate_code_block_auth_context.py
Exit code 0 == fix behavior validated.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
import uuid

from playwright.sync_api import sync_playwright

_SESSIONS: set[str] = set()
LOGIN_HTML = b"<html><body><h1>Sign in</h1><form>sign-in page</form></body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _cookie_session(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            if part.strip().startswith("session="):
                return part.strip()[len("session=") :]
        return None

    def do_GET(self):
        if self.path.startswith("/login"):
            # "Authenticate": mint a session and set it as a cookie.
            sid = uuid.uuid4().hex
            _SESSIONS.add(sid)
            self.send_response(200)
            self.send_header("Set-Cookie", f"session={sid}; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Logged in</h1></body></html>")
        elif self.path.startswith("/dashboard"):
            sid = self._cookie_session()
            if sid and sid in _SESSIONS:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h1 id='who'>WELCOME authed-user</h1></body></html>")
            else:
                # Unauthenticated -> behave like the real app: serve the sign-in page.
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(LOGIN_HTML)
        else:
            self.send_response(404)
            self.end_headers()


def main() -> int:
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
        port = httpd.server_address[1]
        base = f"http://127.0.0.1:{port}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        with sync_playwright() as p:
            browser = p.chromium.launch()

            # --- AGENT BLOCK: authenticate inside its BrowserContext ---
            agent_ctx = browser.new_context()
            agent_page = agent_ctx.new_page()
            agent_page.goto(f"{base}/login")
            assert "Logged in" in agent_page.content(), "agent login failed to set up session"
            print("[agent block]   logged in (session cookie stored in agent BrowserContext)")

            # --- CODE BLOCK (BROKEN, pre-fix): a separate/fresh context ---
            broken_ctx = browser.new_context()
            broken_page = broken_ctx.new_page()
            broken_page.goto(f"{base}/dashboard")
            broken_text = broken_page.content()
            broken_authed = "WELCOME" in broken_text
            print(f"[code BROKEN ]  page.goto(/dashboard) in a fresh context -> "
                  f"{'AUTHED' if broken_authed else 'SIGN-IN PAGE'} (reproduces the bug)")

            # --- CODE BLOCK (FIXED): reuse the agent's BrowserContext ---
            fixed_page = agent_ctx.new_page()
            fixed_page.goto(f"{base}/dashboard")
            who = fixed_page.evaluate("() => document.querySelector('#who') && document.querySelector('#who').textContent")
            fixed_authed = who is not None and "authed-user" in who
            print(f"[code FIXED  ]  page.goto(/dashboard)+page.evaluate() reusing agent context -> "
                  f"{'AUTHED (' + who + ')' if fixed_authed else 'SIGN-IN PAGE'}")

            browser.close()

        ok = (not broken_authed) and fixed_authed
        print("\nRESULT:", "PASS — reusing the agent's context keeps the code block authenticated"
              if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
