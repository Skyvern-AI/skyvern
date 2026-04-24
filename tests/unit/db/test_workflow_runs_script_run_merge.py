"""Regression guards for the `_merge_script_run` helper in
`workflow_runs` repository.

Merge-on-write is load-bearing: callers update different facets of
`workflow_run.script_run` at different points in a run's lifecycle
(setup time writes script identity; mid-execution fallback writes
`ai_fallback_triggered=True`). Without the merge, the second write
clobbers whichever facet wasn't touched, and consumers that read the
`script_run` API field see stale/missing data.

These tests lock in the three invariants:

1. First write into a null column produces the full populated dict.
2. A subsequent partial update preserves unrelated keys.
3. Calls with all nullable params as None are documented no-ops
   (enforced at the caller; this file documents the expectation).
"""

from __future__ import annotations

from skyvern.forge.sdk.db.repositories.workflow_runs import _merge_script_run


def test_merge_script_run_first_write_creates_full_dict() -> None:
    """A server-side code-mode run initializes `script_run` from null.
    Helper called with all three fields → dict has all three keys."""
    result = _merge_script_run(
        existing=None,
        ai_fallback_triggered=False,
        script_id="s_abc",
        script_revision_id="sr_xyz",
    )
    assert result == {
        "ai_fallback_triggered": False,
        "script_id": "s_abc",
        "script_revision_id": "sr_xyz",
    }


def test_merge_script_run_fallback_flip_preserves_identity() -> None:
    """Regression guard for the original motivating bug: after
    `_mark_script_run_loaded` writes identity at setup, a later
    fallback-flip update to `ai_fallback_triggered=True` must NOT
    clobber `script_id` / `script_revision_id`. A replace-based
    update (pre-this-PR behavior) would have emitted
    `{"ai_fallback_triggered": True}` only, breaking consumers that
    read script identity from the API."""
    existing = {
        "ai_fallback_triggered": False,
        "script_id": "s_abc",
        "script_revision_id": "sr_xyz",
    }
    result = _merge_script_run(
        existing=existing,
        ai_fallback_triggered=True,
        script_id=None,
        script_revision_id=None,
    )
    assert result == {
        "ai_fallback_triggered": True,
        "script_id": "s_abc",
        "script_revision_id": "sr_xyz",
    }


def test_merge_script_run_later_identity_write_preserves_fallback_flag() -> None:
    """Symmetric case: if `ai_fallback_triggered` was written first
    (e.g., by a writer that doesn't know about identity yet), a later
    identity write must not clobber the fallback bool."""
    existing = {"ai_fallback_triggered": True}
    result = _merge_script_run(
        existing=existing,
        ai_fallback_triggered=None,
        script_id="s_abc",
        script_revision_id="sr_xyz",
    )
    assert result == {
        "ai_fallback_triggered": True,
        "script_id": "s_abc",
        "script_revision_id": "sr_xyz",
    }


def test_merge_script_run_ignores_none_params() -> None:
    """A call with all three params as None returns the existing dict
    unchanged. The gate in `update_workflow_run` prevents invoking
    `_merge_script_run` in this case, but the helper's own behavior
    must be a no-op for defense in depth."""
    existing = {"ai_fallback_triggered": True, "script_id": "s_abc"}
    result = _merge_script_run(
        existing=existing,
        ai_fallback_triggered=None,
        script_id=None,
        script_revision_id=None,
    )
    assert result == {"ai_fallback_triggered": True, "script_id": "s_abc"}


def test_merge_script_run_false_fallback_is_written_not_skipped() -> None:
    """Guard against the `if ai_fallback_triggered:` pitfall — `False`
    is a meaningful value, not a skip signal. Only `None` should skip."""
    result = _merge_script_run(
        existing={"ai_fallback_triggered": True},
        ai_fallback_triggered=False,
        script_id=None,
        script_revision_id=None,
    )
    assert result == {"ai_fallback_triggered": False}


def test_merge_script_run_empty_dict_existing_same_as_none() -> None:
    """Legacy rows may have `script_run = {}` (empty dict) instead of
    null. Merge treats them identically — the helper uses `or {}` so
    both produce the same starting point."""
    from_none = _merge_script_run(
        existing=None,
        ai_fallback_triggered=False,
        script_id="s_abc",
        script_revision_id=None,
    )
    from_empty = _merge_script_run(
        existing={},
        ai_fallback_triggered=False,
        script_id="s_abc",
        script_revision_id=None,
    )
    assert from_none == from_empty
