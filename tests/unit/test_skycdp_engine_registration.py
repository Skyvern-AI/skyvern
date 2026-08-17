"""The skycdp engine's contract with the per-run engine seam.

Two properties matter here and neither is about CDP: the engine must be *unselectable* by any
production browser source until someone deliberately carves one in, and it must fail *loud* rather
than silently degrade when asked for something it cannot do. The second rule is not hypothetical —
a driver that logged a capability it had quietly dropped once cost this fleet its stealth posture.
"""

from __future__ import annotations

import pytest

from skyvern.webeye.browser_engine import (
    REGISTRY,
    SKYCDP_ALLOWED_BROWSER_SOURCES,
    SKYCDP_ENGINE_NAME,
    SKYCDP_SPEC,
    BrowserSourceNotSupportedByEngine,
)
from skyvern.webeye.skycdp.errors import CdpError, CdpTargetClosedError, CdpTimeoutError, CdpUnsupportedOperation

pytestmark = pytest.mark.asyncio


async def test_engine_is_registered_under_its_name() -> None:
    assert SKYCDP_ENGINE_NAME in REGISTRY.names()
    assert REGISTRY.get(SKYCDP_ENGINE_NAME) is SKYCDP_SPEC


async def test_engine_denies_every_browser_source_including_an_unattributed_one() -> None:
    assert SKYCDP_ALLOWED_BROWSER_SOURCES == frozenset()
    selection = SKYCDP_SPEC.select(selection_reason="test")
    for source in ("cdp-connect", "stealth-chromium", "brightdata", None):
        with pytest.raises(BrowserSourceNotSupportedByEngine):
            selection.ensure_supports(source)


async def test_selection_binds_the_engines_own_error_families() -> None:
    selection = SKYCDP_SPEC.select(selection_reason="test")
    assert selection.error_type is CdpError
    assert selection.timeout_error_type is CdpTimeoutError
    assert selection.is_engine_error(CdpTargetClosedError("gone"))
    assert selection.is_engine_timeout_error(CdpTimeoutError("slow"))
    assert not selection.is_engine_error(ValueError("someone else's error"))


async def test_target_closed_is_classified_rather_than_flattened_into_the_base() -> None:
    selection = SKYCDP_SPEC.select(selection_reason="test")
    classified = selection.classify_error(CdpTargetClosedError("session gone"))
    assert classified is not None
    assert isinstance(classified.__cause__, CdpTargetClosedError)


async def test_spec_reports_installed_because_the_driver_ships_in_tree() -> None:
    assert SKYCDP_SPEC.is_installed() is True


async def test_driver_starts_without_a_subprocess_and_exposes_only_chromium() -> None:
    driver = await SKYCDP_SPEC.select(selection_reason="test").start_driver()
    try:
        assert driver.chromium.name == "chromium"
        assert not hasattr(driver, "firefox")
        assert not hasattr(driver, "webkit")
    finally:
        await driver.stop()


async def test_launching_fails_loud_instead_of_pretending_to_attach() -> None:
    driver = await SKYCDP_SPEC.select(selection_reason="test").start_driver()
    try:
        with pytest.raises(CdpUnsupportedOperation) as launch_error:
            await driver.chromium.launch()
        assert "attach-only" in str(launch_error.value)

        with pytest.raises(CdpUnsupportedOperation):
            await driver.chromium.launch_persistent_context("/tmp/does-not-matter")
    finally:
        await driver.stop()


async def test_a_recording_request_fails_the_run_in_the_attach_only_worker() -> None:
    """Recording used to raise unconditionally, which read as principled and was not.

    Production passes record_video_dir on the main context-creation path, so refusing it meant the
    engine could never create a context at all: a full offline replay produced 0 of 13 runs, every
    one dying here before it reached a page. The refusal now applies where the assumption is actually
    broken -- the worker that never launched a browser, so nothing could have configured recording --
    and elsewhere the capability is dropped with a warning so the engine can still run.
    """
    from skyvern.webeye import attach_only
    from skyvern.webeye.attach_only import AttachOnlyViolation
    from skyvern.webeye.skycdp.facade.browser import Browser

    browser = Browser.__new__(Browser)
    attach_only.enforce_attach_only(True)
    try:
        with pytest.raises(AttachOnlyViolation) as excinfo:
            await Browser.new_context(browser, record_video_dir="/tmp/videos")
        assert "record_video_dir" in str(excinfo.value)
    finally:
        attach_only.enforce_attach_only(False)
