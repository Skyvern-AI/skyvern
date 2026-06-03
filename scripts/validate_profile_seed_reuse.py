"""Semantic validation for Goal 2: seed a browser profile once, reuse it later.

No Skyvern server / LLM / external account required. It models the mechanism
the saved "browser profile" relies on (a Playwright *persistent context* directory
that stores cookies/localStorage) and proves the seed -> reuse flow end to end:

  * SEED  : a persistent context logs in; the session cookie is written into the
            profile directory (this is what the credential-test endpoint does once
            the custom login prompt clears 2FA — Goal 2a).
  * REUSE : a NEW persistent context opened on the SAME profile directory does
            page.goto(/dashboard) + page.evaluate() and is authenticated, with NO
            login block — exactly the "EXTRACT-only run bound to the profile" check.
  * CONTROL: a fresh/empty profile directory lands on the sign-in page.

Run:  uv run python scripts/validate_profile_seed_reuse.py
Exit 0 == seed-and-reuse validated.
"""

from __future__ import annotations

import http.server
import socketserver
import tempfile
import threading
import uuid

from playwright.sync_api import sync_playwright

_SESSIONS: set[str] = set()
LOGIN_HTML = b"<html><body><h1>Sign in</h1></body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _session(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            if part.strip().startswith("session="):
                return part.strip()[len("session=") :]
        return None

    def do_GET(self):
        if self.path.startswith("/login"):
            sid = uuid.uuid4().hex
            _SESSIONS.add(sid)
            self.send_response(200)
            self.send_header("Set-Cookie", f"session={sid}; Path=/; Max-Age=86400")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Logged in</h1></body></html>")
        elif self.path.startswith("/dashboard"):
            sid = self._session()
            if sid and sid in _SESSIONS:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h1 id='who'>WELCOME authed-user</h1></body></html>")
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(LOGIN_HTML)
        else:
            self.send_response(404)
            self.end_headers()


def _authed_on_dashboard(ctx, base: str) -> bool:
    page = ctx.new_page()
    page.goto(f"{base}/dashboard")
    who = page.evaluate("() => document.querySelector('#who') && document.querySelector('#who').textContent")
    return who is not None and "authed-user" in who


def main() -> int:
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
        port = httpd.server_address[1]
        base = f"http://127.0.0.1:{port}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        seed_dir = tempfile.mkdtemp(prefix="seed_profile_")
        empty_dir = tempfile.mkdtemp(prefix="empty_profile_")

        with sync_playwright() as p:
            # SEED: log in inside a persistent profile directory, then close it.
            seed_ctx = p.chromium.launch_persistent_context(seed_dir, headless=True)
            sp = seed_ctx.new_page()
            sp.goto(f"{base}/login")
            assert "Logged in" in sp.content(), "seed login failed"
            seed_ctx.close()
            print("[seed   ]  logged in; session persisted to profile dir")

            # REUSE: brand-new context on the SAME profile dir, no login block.
            reuse_ctx = p.chromium.launch_persistent_context(seed_dir, headless=True)
            reuse_authed = _authed_on_dashboard(reuse_ctx, base)
            reuse_ctx.close()
            print(f"[reuse  ]  goto(/dashboard)+evaluate() on saved profile -> "
                  f"{'AUTHED' if reuse_authed else 'SIGN-IN PAGE'} (extract-only run, no login)")

            # CONTROL: empty profile dir -> unauthenticated.
            ctrl_ctx = p.chromium.launch_persistent_context(empty_dir, headless=True)
            ctrl_authed = _authed_on_dashboard(ctrl_ctx, base)
            ctrl_ctx.close()
            print(f"[control]  goto(/dashboard) on empty profile      -> "
                  f"{'AUTHED' if ctrl_authed else 'SIGN-IN PAGE'}")

        ok = reuse_authed and not ctrl_authed
        print("\nRESULT:", "PASS — a seeded profile authenticates a later run without logging in again"
              if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
