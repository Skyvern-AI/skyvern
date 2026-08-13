"""An attach-only worker must refuse a browser it would have to launch, at startup.

The worker this guards carries no browser binary at all -- that is what lets its image be arm64,
since Chrome and CloakBrowser are x86_64-only on Linux. Configured with a launching browser type it
would not fail at boot but somewhere inside the first run, as an unclassified crash on a code path
that should have been unreachable.
"""

from __future__ import annotations

import pytest

from skyvern.webeye.attach_only import (
    ATTACH_ONLY_BROWSER_TYPES,
    LaunchingBrowserInAttachOnlyWorker,
    assert_attach_only_capable,
    is_attach_only_browser_type,
)


@pytest.mark.parametrize("browser_type", sorted(ATTACH_ONLY_BROWSER_TYPES))
def test_every_attach_source_is_accepted(browser_type: str) -> None:
    assert is_attach_only_browser_type(browser_type) is True
    assert_attach_only_capable(browser_type)


@pytest.mark.parametrize(
    "browser_type",
    ["chromium-headless", "chromium-headful", "stealth-chromium", "chrome-persistent-stealth"],
)
def test_a_launching_source_is_refused_at_startup(browser_type: str) -> None:
    assert is_attach_only_browser_type(browser_type) is False
    with pytest.raises(LaunchingBrowserInAttachOnlyWorker) as excinfo:
        assert_attach_only_capable(browser_type)
    # The message must say what to do, not merely that something is wrong.
    assert browser_type in str(excinfo.value)
    assert "cdp-connect" in str(excinfo.value)


def test_an_unknown_source_is_refused_rather_than_assumed_safe() -> None:
    with pytest.raises(LaunchingBrowserInAttachOnlyWorker):
        assert_attach_only_capable("some-new-browser-type")


class TestEnforcement:
    """With enforcement on, a path that should be unreachable must fail the run, not degrade.

    A stub that returns something plausible is the expensive failure: the run continues, produces a
    wrong result, and nothing in the logs says why. A named exception shows up in a canary as a
    failed run with a cause attached.
    """

    def setup_method(self) -> None:
        from skyvern.webeye.attach_only import enforce_attach_only

        enforce_attach_only(True)

    def teardown_method(self) -> None:
        from skyvern.webeye.attach_only import enforce_attach_only

        enforce_attach_only(False)

    def test_reaching_a_launch_only_path_raises(self) -> None:
        from skyvern.webeye.attach_only import AttachOnlyViolation, forbid

        with pytest.raises(AttachOnlyViolation) as excinfo:
            forbid("Page.video")
        assert "Page.video" in str(excinfo.value)
        # The message must explain the worker's shape, not merely name the symbol.
        assert "already-running browser" in str(excinfo.value)

    def test_page_video_fails_the_run_instead_of_reporting_none(self) -> None:
        from skyvern.webeye.attach_only import AttachOnlyViolation
        from skyvern.webeye.skycdp.facade.page import Page

        page = Page.__new__(Page)
        with pytest.raises(AttachOnlyViolation):
            _ = page.video

    @pytest.mark.asyncio
    async def test_a_local_launch_creator_refuses_before_trying(self) -> None:
        """There is no browser binary in this image, so trying would fail unrecognisably."""
        from skyvern.webeye.attach_only import AttachOnlyViolation
        from skyvern.webeye.browser_factory import _create_headful_chromium, _create_headless_chromium

        for creator in (_create_headless_chromium, _create_headful_chromium):
            with pytest.raises(AttachOnlyViolation):
                await creator(None)


class TestEnforcementOffByDefault:
    """The same code serves the browser-carrying fleet, which must be completely unaffected."""

    def test_page_video_reports_none_when_not_enforcing(self) -> None:
        from skyvern.webeye.attach_only import is_enforcing
        from skyvern.webeye.skycdp.facade.page import Page

        assert is_enforcing() is False
        assert Page.__new__(Page).video is None


class TestVideoListenerGating:
    """Which processes lose popup/main-page video recording.

    The gate used to key on `is_attach_only_browser_type(settings.BROWSER_TYPE)`, on the theory that
    an attached browser was configured by whoever launched it so recording could only no-op. That is
    false -- Playwright records video on a context IT created over `connect_over_cdp` regardless of
    who launched the browser. The consequence was wider than popups: `set_popup_video_listener` is
    the only producer of `video_artifacts`, so any process with an attach-capable BROWSER_TYPE lost
    the main page's recording too.
    """

    def test_a_normal_worker_on_an_attach_capable_browser_type_still_records(self) -> None:
        from skyvern.webeye.attach_only import is_attach_only_browser_type, is_enforcing

        # cdp-connect is attach-CAPABLE, but a normal worker using it can still record.
        assert is_attach_only_browser_type("cdp-connect") is True
        assert is_enforcing() is False, "a normal worker is not enforcing, so it must keep video"

    def test_only_the_attach_only_worker_is_the_one_that_cannot_record(self) -> None:
        from skyvern.webeye import attach_only

        attach_only.enforce_attach_only(True)
        try:
            assert attach_only.is_enforcing() is True
        finally:
            attach_only.enforce_attach_only(False)
        assert attach_only.is_enforcing() is False


class TestTheEntrypointImportsWithoutTheStrippedDrivers:
    """The attach image must be able to START, which is not the same as its modules importing.

    The first version of this image stripped playwright and asserted that
    `skyvern.webeye.attach_only` and `skyvern.webeye.skycdp` were importable. Both were. The worker
    entrypoint was not: it reaches the cloud package, and `cloud/actions.py` imported playwright at
    module level, so every attach pod would have died with ModuleNotFoundError before starting.
    Asserting on leaf modules proved nothing about the thing that actually runs.
    """

    def test_the_real_entrypoint_imports_with_patchright_and_rustwright_absent(self) -> None:
        import subprocess
        import sys
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        probe = (
            "import sys, importlib.abc\n"
            "from skyvern.webeye.attach_only import FORBIDDEN_DRIVER_PACKAGES\n"
            "class B(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in FORBIDDEN_DRIVER_PACKAGES:\n"
            "            raise ModuleNotFoundError(name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, B())\n"
            "import workers.run_worker\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=repo_root, capture_output=True, text=True, timeout=300
        )
        assert "OK" in result.stdout, (
            "the attach worker entrypoint cannot import without the drivers this image strips:\n"
            f"{result.stderr[-2000:]}"
        )

    def test_playwright_is_not_claimed_strippable(self) -> None:
        """Pins the reasoning, so nobody re-adds it to the list and breaks startup again."""
        from skyvern.webeye.attach_only import FORBIDDEN_DRIVER_PACKAGES

        assert "playwright" not in FORBIDDEN_DRIVER_PACKAGES, (
            "playwright cannot be stripped: 83 modules import it at module level and the worker "
            "entrypoint reaches them. The cost this image removes is the driver SUBPROCESS, which an "
            "installed-but-unstarted playwright does not spawn."
        )


def test_the_startup_check_admits_the_dispatch_alias_but_not_a_launching_type() -> None:
    """Startup only. is_attach_only_browser_type is consulted against settings.BROWSER_TYPE at boot
    and by no runtime path, so this pins which BROWSER_TYPE may start a worker -- not what happens
    when a dispatch alias later resolves to a launching leaf. That is
    tests/cloud/test_cloud_browser_factory_vendor_lane_guard.py.
    """
    from skyvern.webeye.attach_only import is_attach_only_browser_type

    assert is_attach_only_browser_type("dynamic-browser")
    for vendor in ("anchor-browser", "browser-use", "remote-cdp-vendor", "cdp-fetch-download-browser"):
        assert is_attach_only_browser_type(vendor), vendor
    for launching in ("chrome-persistent-stealth", "msedge-persistent-stealth", "stealth-chromium"):
        assert not is_attach_only_browser_type(launching), launching
