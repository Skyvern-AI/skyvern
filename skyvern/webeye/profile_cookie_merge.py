"""Union a live browser context's cookies into a browser profile archive, freshest-wins.

Used by the credential living-profile banking engine to heal a credential's profile from runs
that logged in but were seeded elsewhere (no whole-dir cross-writes — no-mixing must hold in the
write direction). The union lands in a sidecar restored via Playwright ``add_cookies`` at boot,
which sidesteps Chromium ``Cookies`` SQLite surgery (no encryption/schema/patchright fragility).
"""

import contextlib
import json
import os

import structlog

LOG = structlog.get_logger()

BANKED_COOKIES_FILENAME = ".skyvern_banked_cookies.json"

# Keys accepted by Playwright's add_cookies; drop anything else (e.g. partitionKey) so one
# unexpected field can't reject the whole batch. Single source — session_cookies imports this.
_ALLOWED_COOKIE_KEYS = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}


def _cookie_key(cookie: dict) -> tuple[str, str, str]:
    return (cookie.get("domain", ""), cookie.get("name", ""), cookie.get("path", ""))


def _sanitize(cookie: dict) -> dict | None:
    if not cookie.get("name") or not cookie.get("domain"):
        return None
    return {k: v for k, v in cookie.items() if k in _ALLOWED_COOKIE_KEYS}


def seed_cookie_values(seed_cookies: list[dict]) -> dict[tuple[str, str, str], str]:
    """Map each seed cookie's (domain, name, path) key to its value — the ``base`` side of the
    delta-merge three-way. A key absent here means the run did not seed that cookie."""
    values: dict[tuple[str, str, str], str] = {}
    for cookie in seed_cookies:
        sanitized = _sanitize(cookie)
        if sanitized is not None:
            values[_cookie_key(sanitized)] = sanitized.get("value", "")
    return values


def union_cookies_into_profile_dir(
    live_cookies: list[dict],
    profile_dir: str,
    base_values: dict[tuple[str, str, str], str] | None = None,
) -> int:
    """Union ``live_cookies`` into ``profile_dir``'s banked-cookies sidecar by (domain, name, path).
    Returns the resulting cookie count (0 when nothing was written).

    Default (``base_values=None``) is incoming-wins: the just-completed verified login is the freshest
    state for its cookies, so the writer always wins on a key clash (Playwright cookies carry no
    per-cookie update time). The credential login-bank path uses this.

    When ``base_values`` (the run's seed values) is given — the delta-merge path — a per-key three-way
    guards the clash: apply an incoming cookie only if the sidecar's current value for that key still
    equals our seed (base == theirs → take ours), or the key is new; if the sidecar diverged from our
    seed, a fresher writer touched it after we seeded, so keep theirs.
    """
    # Three-way is at the plaintext-sidecar grain; a concurrent whole-dir writer that lands
    # a key only in Chromium's SQLite (sidecar cleared) is still incoming-wins — tighten if observed.
    if not profile_dir or not os.path.isdir(profile_dir):
        return 0
    path = os.path.join(profile_dir, BANKED_COOKIES_FILENAME)

    merged: dict[tuple[str, str, str], dict] = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                for cookie in json.load(f):
                    sanitized = _sanitize(cookie)
                    if sanitized is not None:
                        merged[_cookie_key(sanitized)] = sanitized
        except (OSError, ValueError):
            LOG.warning("Discarding unreadable banked-cookies sidecar before union", exc_info=True)

    for cookie in live_cookies:
        sanitized = _sanitize(cookie)
        if sanitized is None:
            continue
        key = _cookie_key(sanitized)
        if base_values is not None and key in merged and merged[key].get("value", "") != base_values.get(key):
            # theirs diverged from our seed → a fresher writer wrote this key → keep theirs.
            continue
        merged[key] = sanitized

    cookies = list(merged.values())
    if not cookies:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        return 0

    # 0o600: the sidecar holds auth cookies. Write to a temp file and atomically replace so a
    # failed write can't leave a partial file or destroy the previous good sidecar.
    tmp = f"{path}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cookies, f)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)
    return len(cookies)


def cookie_delta(end_state_cookies: list[dict], seed_cookies: list[dict]) -> list[dict]:
    """The cookies this run itself changed: end-state entries that are new or value-changed vs the seed
    snapshot, keyed by (domain, name, path). Deletions are intentionally not propagated — a stale run
    must never remove a cookie a concurrent fresher login added.

    Used by the freshness guard to contribute only a run's own cookie changes when the stored profile
    moved under it, so its unchanged (possibly stale) seed cookies can't clobber the concurrent login.
    """
    seed_values = seed_cookie_values(seed_cookies)

    delta: list[dict] = []
    for cookie in end_state_cookies:
        sanitized = _sanitize(cookie)
        if sanitized is None:
            continue
        key = _cookie_key(sanitized)
        if key not in seed_values or seed_values[key] != sanitized.get("value", ""):
            delta.append(sanitized)
    return delta


def clear_banked_cookies(profile_dir: str) -> None:
    """Drop the banked-cookies sidecar. Called before a whole-dir (seed==sink) write so the fresh
    full-profile state supersedes any accumulated cross-run cookie heals."""
    if not profile_dir:
        return
    with contextlib.suppress(FileNotFoundError):
        os.remove(os.path.join(profile_dir, BANKED_COOKIES_FILENAME))
