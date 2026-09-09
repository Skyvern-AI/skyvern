"""Secret-egress filtering for cross-block handoff prose and URLs.

A prior block's finish_reason and final URL are model-facing prose that can carry server-minted
credentials the run's secret registry never saw. Everything that leaves a block for another block's
prompt goes through this module first -- including at persistence time, where a block's own
finish_reason is masked as it is written, because that row is what the next block reads back.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from skyvern.forge.taskv3.opaque_refs import _is_hex_blob, _is_high_entropy_blob, is_signed_url

# Prior-block prose is model output that went through a page: data, never instructions. It is
# rendered inside a labelled data section, single-line, capped, and with signed URLs masked.
MAX_HANDOFF_REASON_CHARS = 300
MAX_HANDOFF_URL_CHARS = 200
# The persisted finish_reason is model prose too: capped at write time, masked of run secrets.
MAX_PERSISTED_FINISH_REASON_CHARS = 2000
_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"\s+")
_TRAILING_PUNCT = ")]}>.,;:!?'\""


def mask_signed_urls_in_text(text: str) -> str:
    """Replace any signing-shaped URL (server-minted tokens the run's secret registry cannot know).
    Trailing prose punctuation is split off the match so it cannot defeat the shape check."""

    def _mask(match: re.Match[str]) -> str:
        url = match.group(0).rstrip(_TRAILING_PUNCT)
        trailer = match.group(0)[len(url) :]
        return ("[signed-url]" if is_signed_url(url) else url) + trailer

    return _URL_RE.sub(_mask, text)


def sanitize_handoff_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    text = mask_signed_urls_in_text(_WS_RE.sub(" ", reason).strip())
    if len(text) > MAX_HANDOFF_REASON_CHARS:
        text = text[: MAX_HANDOFF_REASON_CHARS - 1].rstrip() + "…"
    return text or None


def sanitize_handoff_url(url: str | None) -> str | None:
    """Keep only scheme://host[:port]/path: userinfo, query strings and fragments are where
    credentials and signed tokens live."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not parts.scheme or not hostname:
        return None
    host = f"{hostname}:{port}" if port else hostname
    # A magic link / password reset / signed download can carry its server-minted credential in the
    # PATH, which no secret registry knows; scrub token-shaped segments rather than trust them.
    path = "/".join(
        "***" if segment and (_is_hex_blob(unquote(segment)) or _is_high_entropy_blob(unquote(segment))) else segment
        for segment in parts.path.split("/")
    )
    bare = f"{parts.scheme}://{host}{path}"
    if len(bare) > MAX_HANDOFF_URL_CHARS:
        bare = bare[: MAX_HANDOFF_URL_CHARS - 1] + "…"
    return bare
