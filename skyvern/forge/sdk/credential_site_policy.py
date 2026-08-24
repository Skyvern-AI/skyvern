"""Site identity for deciding where a stored credential's secret may be released.

Shared by the interactive credential fill and code-block execution so both doors
apply one policy (SKY-14103)."""

from __future__ import annotations

import tldextract

from skyvern.forge.sdk.browser_action_policy import _DEFAULT_PORTS, BrowserOrigin, canonicalize_origin

# Private PSL entries are included so hosts that hand strangers a subdomain each -- github.io,
# vercel.app, s3.amazonaws.com -- stay separate sites rather than collapsing into one.
_SITE_EXTRACT = tldextract.TLDExtract(include_psl_private_domains=True)


def origin_of(url: str | None) -> BrowserOrigin | None:
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    return canonicalize_origin(raw)


def site_of(url: str | None) -> tuple[str, int | None, str] | None:
    """A URL's site: scheme and port alongside its domain under the public suffix.

    Scheme and port ride along so a site-wide grant moves between a site's hosts without also
    reaching it over plaintext or on another port, which the origin comparison would refuse.
    """
    origin = origin_of(url)
    if origin is None:
        return None
    extracted = _SITE_EXTRACT(origin.host)
    # tldextract renamed this property; read the new name where the installed version has it.
    site = (
        extracted.top_domain_under_public_suffix
        if hasattr(extracted, "top_domain_under_public_suffix")
        else extracted.registered_domain
    )
    return (origin.scheme, origin.port, site) if site else None


def same_site(current_url: str | None, granted_url: str) -> bool:
    current, granted = site_of(current_url), site_of(granted_url)
    return bool(current and granted and current == granted)


def same_release_scope(current_url: str | None, granted_url: str) -> bool:
    """Same site, or same origin for hosts without a registrable domain (localhost, bare IPs)."""
    if same_site(current_url, granted_url):
        return True
    current, granted = origin_of(current_url), origin_of(granted_url)
    return bool(current and granted and current == granted)


def describe_release_scope(url: str | None) -> str | None:
    """Human-readable name of a URL's release scope, for refusal messages and logs."""
    parts = site_of(url)
    if parts is not None:
        scheme, port, site = parts
        host = site
    else:
        origin = origin_of(url)
        if origin is None:
            return None
        scheme, port, host = origin.scheme, origin.port, origin.host
    if port is None or _DEFAULT_PORTS.get(scheme) == port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"
