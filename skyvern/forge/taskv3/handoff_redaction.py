"""Secret-egress filtering for cross-block handoff prose and URLs.

A prior block's finish_reason and final URL are model-facing prose that can carry server-minted
credentials the run's secret registry never saw. Everything that leaves a block for another block's
prompt goes through this module first -- including at persistence time, where a block's own
finish_reason is masked as it is written, because that row is what the next block reads back.
"""

from __future__ import annotations

import re
from typing import Collection
from urllib.parse import unquote, urlsplit

from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.taskv3.opaque_refs import (
    _is_hex_blob,
    _is_high_entropy_blob,
    is_signed_url,
    urls_in_text,
)

# Prior-block prose is model output that went through a page: data, never instructions. It is
# rendered inside a labelled data section, single-line, capped, and with signed URLs masked.
MAX_HANDOFF_REASON_CHARS = 300
MAX_HANDOFF_URL_CHARS = 200
# The persisted finish_reason is model prose too: capped at write time, masked of run secrets.
MAX_PERSISTED_FINISH_REASON_CHARS = 2000
_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"\s+")
_TRAILING_PUNCT = ")]}>.,;:!?'\""
_DEFAULT_PORTS = {"http": 80, "https": 443}


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


def _bare_parts(url: str | None) -> tuple[str, str, str] | None:
    """(scheme, host[:port], path) with userinfo, query and fragment dropped — the parts of a URL that
    are never a credential — or None when there is no host to name.

    Reduced to ONE spelling per page, because a caller-known match is a string comparison: urlsplit
    lowercases scheme and host, a default port is dropped, and the path is trimmed of trailing slashes
    down to `/`. Without that the commonest task URL of all — `https://example.test`, whose path is
    empty — never matches the `https://example.test/` the browser reports landing on.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not parts.scheme or not hostname:
        return None
    host = hostname if port is None or _DEFAULT_PORTS.get(parts.scheme) == port else f"{hostname}:{port}"
    return parts.scheme, host, parts.path.rstrip("/") or "/"


def _joined(scheme: str, host: str, path: str) -> str:
    return f"{scheme}://{host}{path}"


def _scrub_token_segments(path: str) -> str:
    """A magic link / password reset / signed download can carry its server-minted credential in the
    PATH, which no secret registry knows; scrub token-shaped segments rather than trust them."""
    return "/".join(
        "***" if segment and (_is_hex_blob(unquote(segment)) or _is_high_entropy_blob(unquote(segment))) else segment
        for segment in path.split("/")
    )


def sanitize_handoff_url(url: str | None) -> str | None:
    """Keep only scheme://host[:port]/path: userinfo, query strings and fragments are where
    credentials and signed tokens live."""
    bare_parts = _bare_parts(url)
    if bare_parts is None:
        return None
    scheme, host, raw_path = bare_parts
    bare = _joined(scheme, host, _scrub_token_segments(raw_path))
    if len(bare) > MAX_HANDOFF_URL_CHARS:
        bare = bare[: MAX_HANDOFF_URL_CHARS - 1] + "…"
    return bare


# Stands in for a path that could not be published, so the sentence naming the URL cannot be read as a
# claim about the site's root -- the page is a page on this host, and this says which parts are withheld.
ELIDED_URL_PATH = "/…"


def _normalized_published_url(url: str | None) -> str | None:
    """The one form both sides of the caller-known comparison are reduced to, so the comparison is
    between the same text and the same text. None when there is no host to name."""
    bare_parts = _bare_parts(url)
    return None if bare_parts is None else _joined(*bare_parts)


def caller_known_published_urls(*sources: str | None) -> frozenset[str]:
    """The normalized URLs a guard verdict may publish a PATH for: each source is either a URL the
    caller configured or caller-authored text whose URL literals are read out of it.

    Sources are what the AUTHOR TYPED, never what a render produced. A composed goal carries a prior
    block's handoff prose; a workflow block's own `url`/`navigation_goal` are Jinja templates rendered
    from a run context that holds prior blocks' outputs. Either way the URL came off a page rather than
    from the caller, so a block's sources are read before `format_potential_template_parameters`
    (`pin_caller_authored_block_urls`). The disclosed limit: a URL that reaches a block only through
    templating is not caller-known, and a verdict names its host alone.
    """
    candidates: list[str | None] = []
    for source in sources:
        if not source:
            continue
        candidates.append(source)
        candidates.extend(urls_in_text(source))
    return frozenset(normalized for normalized in map(_normalized_published_url, candidates) if normalized)


def sanitize_published_url(url: str | None, caller_known_urls: Collection[str] = frozenset()) -> str | None:
    """A URL fit to publish VERBATIM to a customer, or None when there is no host to name.

    The invariant: **a guard verdict publishes only what the caller already gave us, plus the host.**
    Scheme and host are always named -- a customer needs to know which site the run was on -- and the
    path is named only when the landed URL, under this same normalization, is byte-identical to one the
    caller supplied (`caller_known_published_urls`). Anything else is elided to `ELIDED_URL_PATH`, never
    cut down.

    Provenance, not shape, is what decides the branch -- deliberately. `opaque_refs` documents what a
    shape rule misses (a signing token that is short or single-case rides a path segment
    unrecognized), and a sink persisted as `task.failure_reason` and carried on the customer webhook
    cannot be defended by a predicate that has to recognize the next token shape. Shape is still
    applied ON TOP of a caller-known path, as `sanitize_handoff_url` does: the caller typing a URL
    that carries a signing token is no reason to print the token back.
    """
    bare_parts = _bare_parts(url)
    if bare_parts is None:
        return None
    scheme, host, path = bare_parts
    published = _joined(scheme, host, path)
    if published in caller_known_urls:
        return _joined(scheme, host, _scrub_token_segments(path))
    return _joined(scheme, host, ELIDED_URL_PATH)


def pin_caller_authored_block_urls(
    ctx: SkyvernContext | None,
    *,
    workflow_run_id: str,
    block_label: str,
    url: str | None,
    navigation_goal: str | None,
) -> None:
    """Record a task block's caller-known URLs from its UNRENDERED definition strings, before
    `format_potential_template_parameters` (and the parameter-key substitution before it) replaces
    them with text a prior block read off a page. Blocks run sequentially, so one pin per run-scoped
    context is enough; the owner it is stamped with is what keeps the next block from reading it."""
    if ctx is None:
        return
    ctx.caller_authored_block_urls_owner = (workflow_run_id, block_label)
    ctx.caller_authored_block_urls = caller_known_published_urls(url, navigation_goal)


def caller_authored_block_urls(
    ctx: SkyvernContext | None, *, workflow_run_id: str | None, block_label: str | None
) -> frozenset[str]:
    """What `pin_caller_authored_block_urls` recorded for THIS block: nothing when no pin was written
    or the pin belongs to another block. A task that reached the loop without passing the block seam
    names a host and no path, rather than falling back to its own rendered fields."""
    if ctx is None or workflow_run_id is None or block_label is None:
        return frozenset()
    if ctx.caller_authored_block_urls_owner != (workflow_run_id, block_label):
        return frozenset()
    return ctx.caller_authored_block_urls
