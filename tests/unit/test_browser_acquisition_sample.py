from __future__ import annotations

import asyncio

import pytest

from skyvern.webeye import browser_acquisition_sample as sample_mod
from skyvern.webeye.browser_acquisition_sample import (
    FALLBACK_TARGET_CLASSICAL_ENGINE,
    FALLBACK_TARGET_LOCAL,
    FALLBACK_TARGET_NONE,
    BrowserAcquisitionSample,
    acquire_first_try_fields,
    begin_browser_acquisition_sample,
    browser_acquisition_scope,
    close_browser_acquisition_sample,
    current_browser_acquisition_sample,
    is_first_try_success,
    note_cdp_connect_attempts,
    note_effective_family,
    note_failure_stage,
    note_fallback_target,
    note_provider_create_attempts,
)


@pytest.fixture(autouse=True)
def _clean_scope() -> None:
    token = sample_mod._current_sample.set(None)
    try:
        yield
    finally:
        sample_mod._current_sample.reset(token)


# --------------------------- predicate --------------------------------------------------------


def test_clean_success_is_first_try() -> None:
    assert is_first_try_success(BrowserAcquisitionSample(provider_create_attempts=1, cdp_connect_attempts=1), True)


def test_directly_selected_local_no_provider_create_is_first_try() -> None:
    # A local launch performs no provider create (attempts stay 0) — a known zero, still first-try.
    assert is_first_try_success(BrowserAcquisitionSample(provider_create_attempts=0, cdp_connect_attempts=1), True)


@pytest.mark.parametrize(
    "sample",
    [
        BrowserAcquisitionSample(provider_create_attempts=2, cdp_connect_attempts=1),
        BrowserAcquisitionSample(provider_create_attempts=1, cdp_connect_attempts=3),
        BrowserAcquisitionSample(
            provider_create_attempts=1, cdp_connect_attempts=1, fallback_target=FALLBACK_TARGET_LOCAL
        ),
        BrowserAcquisitionSample(
            provider_create_attempts=1, cdp_connect_attempts=1, fallback_target=FALLBACK_TARGET_CLASSICAL_ENGINE
        ),
    ],
)
def test_retries_or_fallback_defeat_first_try(sample: BrowserAcquisitionSample) -> None:
    assert is_first_try_success(sample, True) is False


def test_non_success_is_never_first_try() -> None:
    # Missing/unknown data must not read as a zero-retry success: gated on the canonical success.
    assert (
        is_first_try_success(BrowserAcquisitionSample(provider_create_attempts=1, cdp_connect_attempts=1), False)
        is False
    )


# --------------------------- enrichment payload -----------------------------------------------


def test_enrichment_empty_without_scope() -> None:
    assert acquire_first_try_fields(outcome_success=True) == {}


def test_enrichment_fields_and_retry_math() -> None:
    token = begin_browser_acquisition_sample(requested_family="browser-use")
    note_effective_family("browser-use")
    note_provider_create_attempts(3)
    note_cdp_connect_attempts(2)
    fields = acquire_first_try_fields(outcome_success=True)
    close_browser_acquisition_sample(token)
    assert fields["first_try_schema_version"] == sample_mod.FIRST_TRY_SCHEMA_VERSION
    assert fields["provider_create_retry_count"] == 2
    assert fields["cdp_connect_retry_count"] == 1
    assert fields["fallback_target"] == FALLBACK_TARGET_NONE
    assert fields["requested_browser_family"] == "browser-use"
    assert fields["effective_browser_family"] == "browser-use"
    assert fields["first_try_success"] is False  # 3 provider attempts


# --------------------------- recorders / scope ------------------------------------------------


def test_recorders_noop_without_scope() -> None:
    note_provider_create_attempts(5)
    note_cdp_connect_attempts(5)
    note_fallback_target(FALLBACK_TARGET_LOCAL)
    note_failure_stage("whatever")
    assert current_browser_acquisition_sample() is None


def test_recorders_are_monotonic_max() -> None:
    token = begin_browser_acquisition_sample()
    note_cdp_connect_attempts(3)
    note_cdp_connect_attempts(1)
    note_provider_create_attempts(2)
    note_provider_create_attempts(0)
    sample = current_browser_acquisition_sample()
    assert sample is not None and sample.cdp_connect_attempts == 3 and sample.provider_create_attempts == 2
    close_browser_acquisition_sample(token)


def test_fallback_clears_stale_failure_stage() -> None:
    # F5: a stage classification recorded before the degrade must not attribute a later
    # fallback-path failure to the abandoned stage.
    token = begin_browser_acquisition_sample()
    note_failure_stage("provider_create_attempt_timeout")
    note_fallback_target(FALLBACK_TARGET_LOCAL)
    sample = current_browser_acquisition_sample()
    assert sample is not None
    assert sample.failure_stage is None
    assert sample.fallback_target == FALLBACK_TARGET_LOCAL
    close_browser_acquisition_sample(token)


def test_fallback_preserves_provider_count() -> None:
    token = begin_browser_acquisition_sample(requested_family="browser-use")
    note_provider_create_attempts(2)  # vendor retried then failed
    note_fallback_target(FALLBACK_TARGET_LOCAL)
    note_effective_family("stealth-chromium")
    fields = acquire_first_try_fields(outcome_success=True)
    close_browser_acquisition_sample(token)
    assert fields["provider_create_retry_count"] == 1
    assert fields["fallback_target"] == FALLBACK_TARGET_LOCAL
    assert fields["first_try_success"] is False
    assert fields["effective_browser_family"] == "stealth-chromium"


def test_scope_opens_for_create_and_attach_but_not_reuse() -> None:
    # create and attach both open a scope (the final mode is not known until dispatch — a vendor
    # branch may create despite a fallback address); only an explicit reuse opens nothing. Inclusion
    # for a true attach is enforced at emit time by the resolved acquire_mode, not by the scope.
    for mode in ("create", "attach"):
        with browser_acquisition_scope(mode):
            assert current_browser_acquisition_sample() is not None
        assert current_browser_acquisition_sample() is None
    with browser_acquisition_scope("reuse"):
        assert current_browser_acquisition_sample() is None


def test_scope_closes_on_exception() -> None:
    with pytest.raises(RuntimeError):
        with browser_acquisition_scope("create"):
            assert current_browser_acquisition_sample() is not None
            raise RuntimeError("boom")
    assert current_browser_acquisition_sample() is None


def test_closed_sample_rejects_late_updates() -> None:
    token = begin_browser_acquisition_sample()
    sample = current_browser_acquisition_sample()
    assert sample is not None
    close_browser_acquisition_sample(token)
    note_cdp_connect_attempts(99)  # closed scope reset; no current sample
    assert sample.cdp_connect_attempts == 0


@pytest.mark.asyncio
async def test_independent_child_task_gets_its_own_sample() -> None:
    # A child task inherits the parent's ContextVar reference but must own a fresh sample so it does
    # not overwrite the parent's accumulation.
    parent_token = begin_browser_acquisition_sample(requested_family="dynamic-browser")
    note_provider_create_attempts(1)

    async def child() -> int:
        child_token = begin_browser_acquisition_sample(requested_family="stealth-chromium")
        note_provider_create_attempts(3)
        sample = current_browser_acquisition_sample()
        attempts = sample.provider_create_attempts if sample else -1
        close_browser_acquisition_sample(child_token)
        return attempts

    assert await asyncio.create_task(child()) == 3
    parent = current_browser_acquisition_sample()
    assert parent is not None and parent.provider_create_attempts == 1  # untouched by the child
    close_browser_acquisition_sample(parent_token)


def test_resolved_acquire_mode_default_and_override() -> None:
    # No scope / nothing recorded -> the pre-dispatch default stands.
    assert sample_mod.resolve_acquire_mode("attach") == "attach"
    token = begin_browser_acquisition_sample()
    assert sample_mod.resolve_acquire_mode("attach") == "attach"  # recorded nothing yet
    sample_mod.note_resolved_acquire_mode("create")
    assert sample_mod.resolve_acquire_mode("attach") == "create"  # dispatch override wins
    close_browser_acquisition_sample(token)


def test_resolved_create_is_sticky_across_fallback() -> None:
    # A vendor marks create; a later cross-family degrade must not downgrade it to attach.
    token = begin_browser_acquisition_sample()
    sample_mod.note_resolved_acquire_mode("create")
    sample_mod.note_resolved_acquire_mode("attach")  # e.g. a local fallback dialing an address
    assert sample_mod.resolve_acquire_mode("attach") == "create"
    close_browser_acquisition_sample(token)


def test_note_resolved_acquire_mode_noop_without_scope() -> None:
    sample_mod.note_resolved_acquire_mode("create")  # must not raise
    assert current_browser_acquisition_sample() is None
