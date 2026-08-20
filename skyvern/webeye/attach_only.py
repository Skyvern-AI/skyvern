"""The attach-only worker contract: this process never starts a browser.

A worker running this way is always handed a browser that already exists -- a persistent-session id
or a browser address -- so the launch half of the browser stack is not merely unused, it is
unreachable. Saying so explicitly is worth more than it looks:

- It stops the raw-CDP engine being asked to emulate launch-adjacent surface it can never honour.
  ``page.video`` is the cautionary case: recording only exists because a launch configured it, and an
  attach-only worker that still reaches for it gets an attribute error on a code path that has no
  business running at all. The answer is not a better stub, it is not asking.
- It lets the image drop the browser entirely, which is what makes an arm64 build possible: Chrome
  and CloakBrowser are x86_64-only on Linux, so a worker that carries a browser is pinned to x86_64
  and one that does not is free.
- It converts a whole class of mid-run failure into a startup failure, which is the only kind worth
  having.
"""

from __future__ import annotations

from skyvern.exceptions import SkyvernException

# Browser sources that hand this process an already-running browser. Everything else launches one.
ATTACH_ONLY_BROWSER_TYPES = frozenset(
    {
        "cdp-connect",
        "cdp-connection-browser",
        "cdp-fetch-download-browser",
        "brightdata",
        "browserbase",
        "undetect-io",
        "remote-cdp-vendor",
        "browser-use",
        "anchor-browser",
        "persistent-cdp-browser",
    }
)


# Driver packages that must not be INSTALLED in an attach-only image. Kept here rather than spelled
# out in the Dockerfile so the image asserts against the same list the code reasons about.
#
# playwright is deliberately absent from this list, and the reason is worth recording because the
# first version of this image did try to strip it. It cannot be: the worker entrypoint imports the
# cloud package, and 83 modules across cloud/ and skyvern/ import playwright at module level, many
# of them for genuine runtime use (async_playwright(), `except PlaywrightError`). Stripping it made
# every attach pod die with ModuleNotFoundError before the worker started.
#
# Excluding the package was never where the benefit was. The cost this image exists to remove is the
# node DRIVER SUBPROCESS -- one per concurrent run, ~110 MB each -- and that is spawned by
# async_playwright().start(), which the attach-only engine never calls. An installed-but-unstarted
# playwright costs image bytes, not pod memory. patchright and rustwright ARE excluded, because
# nothing imports them at module level; both are loaded lazily inside the functions that start them.
FORBIDDEN_DRIVER_PACKAGES: tuple[str, ...] = ("patchright", "rustwright")


class AttachOnlyViolation(SkyvernException):
    """Raised when an attach-only worker reaches a code path that can only exist for a launched browser.

    These paths are unreachable by design, so reaching one means an assumption broke. Failing the run
    is the point: the alternative is a stub that returns something plausible, and a worker that
    quietly does the wrong thing is far more expensive than one that stops. A canary sees this as a
    failed run with a named cause; a silent degradation it would not see at all.
    """

    def __init__(self, what: str, detail: str = "") -> None:
        self.what = what
        suffix = f" {detail}" if detail else ""
        super().__init__(
            f"Attach-only worker reached {what}, which only exists for a browser this process "
            f"launched. This worker is handed an already-running browser (a persistent-session id or "
            f"a browser address) and carries no browser binary.{suffix}"
        )


class LaunchingBrowserInAttachOnlyWorker(SkyvernException):
    """Raised at startup when an attach-only worker is configured with a browser it would launch."""

    def __init__(self, browser_type: str) -> None:
        self.browser_type = browser_type
        super().__init__(
            f"This worker is attach-only, but BROWSER_TYPE is {browser_type!r}, which starts a browser. "
            f"Attach-only workers are handed a persistent-session id or a browser address; they carry no "
            f"browser binary and cannot launch one. Set BROWSER_TYPE to one of "
            f"{sorted(ATTACH_ONLY_BROWSER_TYPES)}, or run this deployment without attach-only mode."
        )


# Dispatch aliases, which resolve to a concrete type per run rather than naming one. Legal here even
# though some leaves launch: the run-time guard on every launching creator is what refuses those, and
# a fleet pinned to one concrete vendor could not honour a per-run vendor choice at all.
ATTACH_ONLY_DISPATCH_BROWSER_TYPES = frozenset({"dynamic-browser"})


def is_attach_only_browser_type(browser_type: str) -> bool:
    return browser_type in ATTACH_ONLY_BROWSER_TYPES or browser_type in ATTACH_ONLY_DISPATCH_BROWSER_TYPES


def assert_attach_only_capable(browser_type: str) -> None:
    """Fail startup rather than let a launch attempt surface mid-run as an unclassified crash."""
    if not is_attach_only_browser_type(browser_type):
        raise LaunchingBrowserInAttachOnlyWorker(browser_type)


def records_video(record_video_dir: str | None) -> bool:
    """Whether this run records video at all.

    Recording is configured at launch, so an attach-only worker never does. Callers use this to skip
    registering the artifact plumbing rather than registering a listener that can only no-op.
    """
    return bool(record_video_dir)


# Enforcement is a per-process switch rather than a constant, because the same code serves both the
# attach-only worker and the existing browser-carrying fleet. Off, everything behaves as it always
# has; on, the paths that should be unreachable raise instead of degrading.
_enforcing = False


def enforce_attach_only(enabled: bool = True) -> None:
    global _enforcing
    _enforcing = enabled


def is_enforcing() -> bool:
    return _enforcing


def forbid(what: str, detail: str = "") -> None:
    """Fail the run if this process is an attach-only worker; otherwise do nothing."""
    if _enforcing:
        raise AttachOnlyViolation(what, detail)
