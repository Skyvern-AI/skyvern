# code_host — Wyrmhold

A code-hosting site with two repos under one SPA (`index.html`, repo parsed from the URL
path), built for **SKY-12764** to exercise a public-vs-private repo split and a
passkey-first two-factor chain.

| Repo | Entry | Auth |
| --- | --- | --- |
| `Skyvern-AI/skyvern` (Public) | `/code_host/skyvern/` | none |
| `Skyvern-AI/skyvern-cloud` (Private) | `/code_host/skyvern-cloud/` | sign-in + 2FA, always on |

Both repos have a **Code** home (README render, file rows, About sidebar) and an
**Insights → Pulse** view (period heading, Overview stat bars, and the
"Excluding merges, N authors have pushed M commits…" summary). Tab switches are in-SPA;
the entry URL is never rewritten (Skyvern re-anchors on it between steps).

## The properties that make this fixture worth having

- **The logged-out private repo is a 404, not a login redirect.** The real host hides the
  repo's existence: "This is not the web page you are looking for.", title "Page not
  found", an inline **Sign in** popover, and a find-code search. The repo name appears
  nowhere on that page, and a test pins that.
- **2FA is passkey-first, with the authenticator hidden behind "More options".** The code
  screen is not reachable directly; the agent must decline the passkey path. This is a
  different discovery problem from the analytics_console tenants, where the token field
  is the first and only 2FA surface.
- **The code input auto-verifies at six digits** (mirroring the real input's
  auto-submit) — an agent that fills the code and then hunts for a Verify button will
  find the page already moved on.

## Auth contracts (from saved DOM captures)

- Sign-in: `input name="login" id="login_field" autocomplete="username"`,
  `input name="password" id="password" autocomplete="current-password"`,
  `input type="submit" name="commit" value="Sign in"`, "Username or email address",
  "Forgot password?", "or continue with other methods". Wrong credentials show
  `Incorrect username or password.`
- 2FA landing: `h1` "Two-factor authentication", "Authenticate using your passkey.",
  a passkey button (declines with a partial-support notice), and **More options**
  expanding Authenticator app / Recovery code.
- Authenticator screen: "Enter the code from your two-factor authentication app or
  browser extension below.", sr-only label "Enter the verification code",
  `input name="app_otp" id="app_totp" inputmode="numeric"
  pattern="([0-9]{6})|([0-9a-fA-F]{5}-?[0-9a-fA-F]{5})" placeholder="XXXXXX"`,
  **Verify** button. Any six digits pass, so any authenticator seed on the saved
  credential works (`mock-portal-login-totp`).

Demo login: `demo_business_user` / `Demo!Pass123`, matching the repo-wide mock login.

## Evidence basis

Auth pages, the 404, and the Pulse layout follow user-saved DOM captures of the real
host (login, both 2FA screens, logged-out private repo, repo home, both Pulse pages).
The captures are real vendor markup and stay out of the repo per the `wpid-to-fixture`
privacy rule. Branding ("Wyrmhold") is fixture-owned.

Repo content: the public repo's README text and Pulse summary are our own open-source
project's (public data). The **private repo's Pulse numbers are fixture-owned** —
synthesized, not the real private repo's — so no internal activity data lands in the
mock. Structure matches the capture; numbers do not.
