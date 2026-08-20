"""Refuse releasing a code-block credential secret to a page outside the credential's site (SKY-14103)."""

from __future__ import annotations

from dataclasses import dataclass
from os import PathLike, fspath
from typing import Any

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.credential_site_policy import describe_release_scope, origin_of, same_release_scope

LOG = structlog.get_logger()

# Mirrors the masking rule in context_manager: substring-matching a short value (a CVV, a
# 2-digit expiry) misfires on unrelated scalars, so short secrets match on equality only.
_SECRET_SUBSTRING_MIN_LENGTH = 5

# Operations that release a caller-supplied text value into the page; the credential release
# guard runs on these before the value leaves the process.
_VALUE_RELEASE_NAMES = frozenset(
    {
        "locator.fill",
        "locator.type",
        "locator.press_sequentially",
        "page.fill",
        "page.type",
        "page.fill_autocomplete",
        "keyboard.type",
        "keyboard.insert_text",
    }
)


def _string_value(value: Any) -> str | None:
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, PathLike):
        return fspath(value)
    return None


def _arg(args: tuple[Any, ...], index: int) -> Any:
    return args[index] if len(args) > index else None


async def _release_target_url(target: Any, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """The URL of the document that would receive the value: the target element's owner frame."""
    locator = target
    if name.startswith("page."):
        selector = _string_value(kwargs.get("selector", _arg(args, 0)))
        if not selector:
            # A prompt-only fill (SkyvernPage picks the element itself) names no selector to
            # resolve. The document it will act on is this page, so judge that rather than refuse
            # a fill that may well be on the credential's own site.
            return _string_value(target.url)
        locator = target.locator(selector).first
    # An ElementHandle is already resolved and has no element_handle() of its own.
    if not hasattr(locator, "element_handle"):
        handle = locator
    else:
        # Honour the caller's own timeout: resolving here on a slower budget than the fill that
        # follows would turn a login page that is merely slow into a credential-fill failure.
        timeout = kwargs.get("timeout")
        try:
            handle = await locator.element_handle(
                timeout=timeout if isinstance(timeout, (int, float)) else settings.BROWSER_ACTION_TIMEOUT_MS
            )
        except Exception:
            # The operation may still resolve this element its own way (SkyvernPage retries with
            # AI). Judge the page rather than failing a fill the caller could have recovered.
            owner = target if name.startswith("page.") else getattr(locator, "page", None)
            return _string_value(getattr(owner, "url", None))
    if handle is None:
        return None
    frame = await handle.owner_frame()
    return frame.url if frame is not None else None


async def _focused_frame_url(page: Any) -> str | None:
    """The URL of the frame holding focus, so a keystroke into a cross-origin iframe is judged
    against that document rather than against the top-level page that embeds it.

    `document.hasFocus()` is also true for every ancestor of the focused document, so the deepest
    match is the one that receives the keystrokes; an ancestor's activeElement is the <iframe>.
    """
    try:
        frames = list(page.frames)
    except Exception:
        return page.url
    focused: Any = None
    unreadable: list[Any] = []
    for frame in frames:
        try:
            if await frame.evaluate(
                "document.hasFocus() && !['IFRAME', 'FRAME'].includes("
                "(document.activeElement && document.activeElement.tagName) || '')"
            ):
                focused = frame
        except Exception:
            unreadable.append(frame)
            continue
    if focused is not None:
        return focused.url
    # Nothing claimed focus, which is ordinary when the window is not focused at all, so it cannot
    # refuse by itself. Only a frame we could not read can be hiding focus somewhere off-site; a
    # readable frame already answered, and third-party frames a login page merely embeds are not
    # grounds to refuse a keystroke on the page's own site.
    if any(not same_release_scope(frame.url, page.url) for frame in unreadable):
        return None
    return page.url


class CodeBlockCredentialReleaseError(Exception):
    # Duck-typed marker consumed by the secure runner's transport map (same convention as
    # RecordingLocator._skyvern_brokerable_handle): a refusal must cross the protocol as a
    # denied operation, not a healable browser failure.
    skyvern_denied_page_operation = True


@dataclass(frozen=True)
class ArmedSecret:
    secret_value: str
    allowed_url: str
    parameter_key: str


class CredentialReleaseGuard:
    def __init__(self, *, workflow_run_id: str | None = None, block_label: str | None = None) -> None:
        self._armed: list[ArmedSecret] = []
        self._workflow_run_id = workflow_run_id
        self._block_label = block_label

    def arm(self, secret_value: object, allowed_url: object, parameter_key: str) -> bool:
        """Arm one secret, or report that its saved login site yields no scope to compare against."""
        if not (isinstance(secret_value, str) and secret_value and isinstance(allowed_url, str)):
            return False
        scoped = allowed_url.strip()
        if not scoped or describe_release_scope(scoped) is None:
            return False
        self._armed.append(ArmedSecret(secret_value, scoped, parameter_key))
        return True

    @property
    def is_armed(self) -> bool:
        return bool(self._armed)

    def log_armed(self) -> None:
        scopes = sorted({describe_release_scope(entry.allowed_url) or "unreadable" for entry in self._armed})
        LOG.info(
            "codeblock_credential_release_armed",
            parameter_keys=sorted({entry.parameter_key for entry in self._armed}),
            allowed_scopes=scopes,
            secret_count=len(self._armed),
            workflow_run_id=self._workflow_run_id,
            block_label=self._block_label,
        )

    def match(self, value: object) -> ArmedSecret | None:
        matches = self.matches(value)
        return matches[0] if matches else None

    def matches(self, value: object) -> list[ArmedSecret]:
        if not isinstance(value, str) or not value:
            return []
        found: list[ArmedSecret] = []
        for entry in self._armed:
            if len(entry.secret_value) >= _SECRET_SUBSTRING_MIN_LENGTH:
                if entry.secret_value in value:
                    found.append(entry)
            elif value == entry.secret_value:
                found.append(entry)
        return found

    async def enforce(self, target: Any, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        """Refuse `name` before it runs if it would release an armed secret off-site; `target` is
        the receiving locator for locator.* names and the page for page.* / keyboard.* names."""
        if name not in _VALUE_RELEASE_NAMES:
            return
        value_index = 1 if name.startswith("page.") else 0
        raw_value = kwargs.get("value", kwargs.get("text", _arg(args, value_index)))
        candidates = self.matches(raw_value)
        if not candidates:
            return
        entry = candidates[0]
        if name.startswith("keyboard."):
            url = await _focused_frame_url(target)
        else:
            url = await _release_target_url(target, name, args, kwargs)
        self.check_release(entry, url, operation=name, alternatives=candidates[1:])

    def check_release(
        self,
        entry: ArmedSecret,
        target_url: str | None,
        *,
        operation: str,
        alternatives: list[ArmedSecret] | None = None,
    ) -> None:
        # Two credentials can legitimately hold the SAME value (one username saved against two
        # sites), and either owner may authorize it. A shorter secret merely appearing inside the
        # value being filled is a different secret, though, and cannot authorize releasing the
        # longer one: site A's "hunter2!" must not ride out on site B's "hunter2".
        candidates = [entry, *(alternatives or [])]
        refused = entry
        for secret_value in dict.fromkeys(candidate.secret_value for candidate in candidates):
            owners = [candidate for candidate in candidates if candidate.secret_value == secret_value]
            if any(target_url and same_release_scope(target_url, owner.allowed_url) for owner in owners):
                continue
            refused = owners[0]
            break
        else:
            return
        entry = refused
        # Never fall back to the stored URL itself: it can carry basic-auth credentials or a
        # token in its query, and this string reaches both the logs and the run record.
        allowed_scope = describe_release_scope(entry.allowed_url) or "an unreadable saved login site"
        actual_scope = describe_release_scope(target_url) or "a page whose site could not be identified"
        actual_origin = origin_of(target_url)
        LOG.warning(
            "codeblock_credential_release_refused",
            parameter_key=entry.parameter_key,
            allowed_scope=allowed_scope,
            actual_scope=actual_scope,
            # The host is the actionable fact for a repair; the message stays at site granularity.
            actual_host=actual_origin.host if actual_origin is not None else None,
            operation=operation,
            workflow_run_id=self._workflow_run_id,
            block_label=self._block_label,
        )
        raise CodeBlockCredentialReleaseError(
            f"Refused to type the saved credential `{entry.parameter_key}` here: the credential "
            f"belongs to {allowed_scope}, but this field is on {actual_scope}. "
            "Continue the sign-in on the credential's own site instead."
        )
