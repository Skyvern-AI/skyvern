from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.db.enums import BrowserSeedSource
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager, WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import LoginBlock
from skyvern.forge.sdk.workflow.models.parameter import (
    CredentialParameter,
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.runs import ProxyLocation
from skyvern.services.workflow_service import workflow_request_body_from_existing_run


def _workflow(*, persist: bool = False, pick: str | None = None, key: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        persist_browser_session=persist,
        browser_profile_id=pick,
        browser_profile_key=key,
        pin_saved_session_ip=False,
        workflow_permanent_id="wpid_test",
        title="Workflow",
        workflow_definition=SimpleNamespace(blocks=[]),
    )


def _run(retried_from_workflow_run_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_run_id="wr_test",
        organization_id="o_test",
        browser_session_id=None,
        browser_profile_id=None,
        browser_seed_source=None,
        browser_sink_profile_id=None,
        retried_from_workflow_run_id=retried_from_workflow_run_id,
        proxy_location=None,
    )


def _svc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    managed: str | None = None,
    has_content: bool = False,
    credential: str | None = None,
    pick_role: str = "plain",
    pick_owner: str | None = None,
) -> WorkflowService:
    svc = WorkflowService()
    monkeypatch.setattr(svc, "_ensure_managed_browser_profile", AsyncMock(return_value=managed))
    monkeypatch.setattr(svc, "_managed_browser_profile_has_content", AsyncMock(return_value=has_content))
    monkeypatch.setattr(svc, "_resolve_credential_browser_profile_id_for_setup", AsyncMock(return_value=credential))
    monkeypatch.setattr(svc, "_resolve_picked_profile_role", AsyncMock(return_value=(pick_role, pick_owner)))
    return svc


async def _resolve(
    svc: WorkflowService,
    workflow: SimpleNamespace,
    *,
    override: str | None = None,
    start_fresh: bool = False,
    engine_enabled: bool = True,
) -> tuple[str | None, BrowserSeedSource, str | None]:
    # The canonical C table is the engine-ON behavior, so default engine_enabled=True; flag-off variants
    # pass engine_enabled=False (setup-time credential seeding is gated, F3).
    return await svc._resolve_run_seed(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run=_run(),  # type: ignore[arg-type]
        parameter_values={},
        explicit_request_browser_profile_id=override,
        start_fresh=start_fresh,
        engine_enabled=engine_enabled,
    )


# --- C behavior table: (toggle, pick, role) -> (seed, source, sink) ----------
# Canonical table: cloud_docs/design/2026-07-16-browser-profile-picker-design.md (quick-win redesign).
# An explicit pick "always starts there" and never forks a hidden own profile; the sink is the profile
# THIS workflow writes (own auto-profile, or a plain pick with save on) — None means no workflow write.


@pytest.mark.asyncio
async def test_row1_off_no_pick_no_cred_is_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _svc(monkeypatch)
    assert await _resolve(svc, _workflow()) == (None, BrowserSeedSource.fresh, None)


@pytest.mark.asyncio
async def test_row2_off_no_pick_credential_seeds_readonly(monkeypatch: pytest.MonkeyPatch) -> None:
    # Login credential, save off: seed the credential's profile, no workflow sink (heal engine only).
    svc = _svc(monkeypatch, credential="bp_cred")
    assert await _resolve(svc, _workflow()) == ("bp_cred", BrowserSeedSource.credential, None)


@pytest.mark.asyncio
async def test_plain_pick_is_living_under_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    # v32 (B1): a plain pick is always LIVING under the engine (seed==sink); the persist toggle drops out.
    svc = _svc(monkeypatch, pick_role="plain")
    assert await _resolve(svc, _workflow(pick="bp_pick")) == ("bp_pick", BrowserSeedSource.picked, "bp_pick")


@pytest.mark.asyncio
async def test_plain_pick_read_only_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag-off: legacy byte-for-byte — a plain pick is read-only (golden image) unless persist is on.
    svc = _svc(monkeypatch, pick_role="plain")
    assert await _resolve(svc, _workflow(pick="bp_pick"), engine_enabled=False) == (
        "bp_pick",
        BrowserSeedSource.picked,
        None,
    )
    assert await _resolve(svc, _workflow(pick="bp_pick", persist=True), engine_enabled=False) == (
        "bp_pick",
        BrowserSeedSource.picked,
        "bp_pick",
    )


@pytest.mark.asyncio
async def test_row4_on_no_pick_no_cred_forks_own(monkeypatch: pytest.MonkeyPatch) -> None:
    # Save on, no pick, no credential: run 1 seeds fresh; the own auto-profile is the sink.
    svc = _svc(monkeypatch, managed="bp_own", has_content=False, credential=None)
    assert await _resolve(svc, _workflow(persist=True)) == (None, BrowserSeedSource.own_memory, "bp_own")


@pytest.mark.asyncio
async def test_row4_on_no_pick_own_has_content_seeds_own(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _svc(monkeypatch, managed="bp_own", has_content=True)
    assert await _resolve(svc, _workflow(persist=True)) == ("bp_own", BrowserSeedSource.own_memory, "bp_own")


@pytest.mark.asyncio
async def test_row5_on_no_pick_credential_run1_seeds_cred_sinks_own(monkeypatch: pytest.MonkeyPatch) -> None:
    # Save on + login credential, own empty: run 1 boots the credential's profile, forks into own (sink).
    svc = _svc(monkeypatch, managed="bp_own", has_content=False, credential="bp_cred")
    assert await _resolve(svc, _workflow(persist=True)) == ("bp_cred", BrowserSeedSource.credential, "bp_own")


# --- fail-safe on transient infra errors (Aron) ------------------------------


@pytest.mark.asyncio
async def test_picked_lookup_error_preserves_pick_readonly(monkeypatch: pytest.MonkeyPatch) -> None:
    # A transient role-lookup failure ("error") must NOT silently reroute an explicit pick to own-auto:
    # preserve the pick as the seed with no workflow sink (read-only).
    svc = _svc(monkeypatch, pick_role="error", managed="bp_own")
    assert await _resolve(svc, _workflow(pick="bp_pick", persist=True)) == (
        "bp_pick",
        BrowserSeedSource.picked,
        None,
    )


@pytest.mark.asyncio
async def test_deleted_pick_flag_off_preserves_pick_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Lawy: flag-off must NOT fall a deleted/cross-org pick through into the workflow's own managed
    # profile (v32). Legacy kept the configured pick as the seed and wrote the legacy session archive
    # (sink None) — never building bp_own. The seed asserts bp_pick (not bp_own) to prove that.
    svc = _svc(monkeypatch, pick_role="missing", managed="bp_own", has_content=True)
    assert await _resolve(svc, _workflow(pick="bp_pick", persist=True), engine_enabled=False) == (
        "bp_pick",
        BrowserSeedSource.picked,
        None,
    )


@pytest.mark.asyncio
async def test_deleted_pick_flag_on_falls_through_to_own(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag-on keeps the shipped v32 fall-through: a deleted pick resolves to the workflow's own memory.
    svc = _svc(monkeypatch, pick_role="missing", managed="bp_own", has_content=True)
    assert await _resolve(svc, _workflow(pick="bp_pick", persist=True)) == (
        "bp_own",
        BrowserSeedSource.own_memory,
        "bp_own",
    )


@pytest.mark.asyncio
async def test_has_content_fail_safe_on_storage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A flaky storage probe must not reseed a Save & Reuse run to fresh and overwrite its accumulated memory.
    svc = WorkflowService()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.service.app.STORAGE.browser_profile_exists",
        AsyncMock(side_effect=RuntimeError("s3 blip")),
    )
    assert await svc._managed_browser_profile_has_content(browser_profile_id="bp_own", organization_id="o") is True


@pytest.mark.asyncio
async def test_picked_profile_role_lookup_failure_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A DB blip during classification returns "error" (caller preserves the pick), not "missing" (reroute).
    svc = WorkflowService()
    monkeypatch.setattr(
        "skyvern.forge.sdk.workflow.service.app.DATABASE.browser_sessions.get_browser_profile",
        AsyncMock(side_effect=RuntimeError("db blip")),
    )
    role, owner = await svc._resolve_picked_profile_role(
        browser_profile_id="bp_pick", workflow=_workflow(), organization_id="o"
    )
    assert (role, owner) == ("error", None)


@pytest.mark.asyncio
async def test_row6_on_plain_pick_is_living_pick_seed_equals_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    # Save on + a plain pick: the pick is both seed and sink (living pick, seed==sink).
    svc = _svc(monkeypatch, pick_role="plain")
    assert await _resolve(svc, _workflow(persist=True, pick="bp_pick")) == (
        "bp_pick",
        BrowserSeedSource.picked,
        "bp_pick",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("persist", [True, False])
async def test_row7_credential_owned_pick_heal_only_no_sink(monkeypatch: pytest.MonkeyPatch, persist: bool) -> None:
    # Picking a credential-owned profile: seed it every run, never a workflow sink (heal engine writes).
    svc = _svc(monkeypatch, pick_role="credential", pick_owner="cred_1")
    assert await _resolve(svc, _workflow(persist=persist, pick="bp_cred")) == (
        "bp_cred",
        BrowserSeedSource.picked,
        None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("persist", [True, False])
async def test_row8_foreign_workflow_pick_live_feed_no_sink(monkeypatch: pytest.MonkeyPatch, persist: bool) -> None:
    # Another workflow's own auto-profile: read-only live feed, owner alone writes.
    svc = _svc(monkeypatch, pick_role="foreign_auto")
    assert await _resolve(svc, _workflow(persist=persist, pick="bp_other")) == (
        "bp_other",
        BrowserSeedSource.picked,
        None,
    )


@pytest.mark.asyncio
async def test_row9_on_no_pick_with_key_sinks_own(monkeypatch: pytest.MonkeyPatch) -> None:
    # Code expression (browser_profile_key) with save on: the per-key own auto-profile is the sink.
    svc = _svc(monkeypatch, managed="bp_own_key", has_content=True)
    assert await _resolve(svc, _workflow(persist=True, key="{{ env }}")) == (
        "bp_own_key",
        BrowserSeedSource.own_memory,
        "bp_own_key",
    )


# --- one-run overrides + fall-through --------------------------------------------------


@pytest.mark.asyncio
async def test_start_fresh_short_circuits_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _svc(monkeypatch, managed="bp_own", credential="bp_cred", pick_role="plain")
    workflow = _workflow(persist=True, pick="bp_pick")
    assert await _resolve(svc, workflow, override="bp_over", start_fresh=True) == (None, BrowserSeedSource.fresh, None)


@pytest.mark.asyncio
async def test_explicit_override_writes_under_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    # v32 (B5): a run-form override is a pick-for-that-run — a plain override writes on success (sink=itself).
    svc = _svc(monkeypatch, managed="bp_own", credential="bp_cred", pick_role="plain")
    workflow = _workflow(persist=True, pick="bp_pick")
    assert await _resolve(svc, workflow, override="bp_over") == ("bp_over", BrowserSeedSource.override, "bp_over")


@pytest.mark.asyncio
async def test_override_read_only_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag-off: legacy one-run read-only override (no sink), byte-for-byte.
    svc = _svc(monkeypatch, pick_role="plain")
    assert await _resolve(svc, _workflow(persist=True, pick="bp_pick"), override="bp_over", engine_enabled=False) == (
        "bp_over",
        BrowserSeedSource.override,
        None,
    )


@pytest.mark.asyncio
async def test_credential_override_heal_only_under_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    # v32: a credential-owned override stays heal-only (no workflow sink) even under the engine.
    svc = _svc(monkeypatch, pick_role="credential")
    assert await _resolve(svc, _workflow(), override="bp_cred_over") == (
        "bp_cred_over",
        BrowserSeedSource.override,
        None,
    )


@pytest.mark.asyncio
async def test_pick_beats_browser_profile_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Correction 3: an explicit plain pick wins deterministically over browser_profile_key. The key only
    # selects WHICH own auto-profile applies in the no-pick rows, so it is ignored (never ensured) here.
    svc = _svc(monkeypatch, managed="bp_own_key", has_content=True, pick_role="plain")
    workflow = _workflow(persist=True, pick="bp_pick", key="{{ env }}")
    assert await _resolve(svc, workflow) == ("bp_pick", BrowserSeedSource.picked, "bp_pick")
    svc._ensure_managed_browser_profile.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_missing_pick_falls_through_to_no_pick_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    # A deleted / cross-org pick (role "missing") falls through to the credential/fresh chain.
    svc = _svc(monkeypatch, credential="bp_cred", pick_role="missing")
    assert await _resolve(svc, _workflow(pick="bp_deleted")) == ("bp_cred", BrowserSeedSource.credential, None)


@pytest.mark.asyncio
async def test_managed_rollback_falls_through_to_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    # persist on but the managed ensure rolled back: no own sink; the credential seeds read-only.
    svc = _svc(monkeypatch, managed=None, credential="bp_cred")
    assert await _resolve(svc, _workflow(persist=True)) == ("bp_cred", BrowserSeedSource.credential, None)


@pytest.mark.asyncio
async def test_managed_rollback_no_credential_is_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _svc(monkeypatch, managed=None, credential=None)
    assert await _resolve(svc, _workflow(persist=True)) == (None, BrowserSeedSource.fresh, None)


@pytest.mark.asyncio
async def test_flag_off_credential_row_resolves_fresh_not_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    # F3: setup-time credential seeding is engine-gated. Flag-off, a save-off credential workflow
    # resolves fresh (today's behavior) and the preserved mid-run stamp loads the credential.
    svc = _svc(monkeypatch, credential="bp_cred")
    assert await _resolve(svc, _workflow(), engine_enabled=False) == (None, BrowserSeedSource.fresh, None)


@pytest.mark.asyncio
async def test_flag_off_persist_seeds_managed_profile_not_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    # F3 + M: flag-off, save-on run 1 seeds the workflow's own MANAGED profile (not the credential
    # fall-through, and not a NULL seed) so the legacy finalization writes it — today's Save & Reuse
    # first-run behavior byte-for-byte. Engine-on it would seed fresh (row 4) with the sink populating own.
    svc = _svc(monkeypatch, managed="bp_own", has_content=False, credential="bp_cred")
    assert await _resolve(svc, _workflow(persist=True), engine_enabled=False) == (
        "bp_own",
        BrowserSeedSource.own_memory,
        "bp_own",
    )


@pytest.mark.asyncio
async def test_fallback_retry_seeds_fresh_ignoring_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # A credential-fallback retry sheds all browser handles for a clean session (credential_fallback.py),
    # so the resolver must not re-seed the workflow's config pick / own memory / credential — it seeds
    # fresh even when every config layer would otherwise resolve a profile.
    svc = _svc(monkeypatch, managed="bp_own", has_content=True, credential="bp_cred", pick_role="plain")
    result = await svc._resolve_run_seed(
        workflow=_workflow(persist=True, pick="bp_pick"),  # type: ignore[arg-type]
        workflow_run=_run(retried_from_workflow_run_id="wr_orig"),  # type: ignore[arg-type]
        parameter_values={},
        explicit_request_browser_profile_id=None,
    )
    assert result == (None, BrowserSeedSource.fresh, None)


# --- stamping + provenance ---------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_and_stamp_writes_profile_source_and_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _svc(monkeypatch, managed="bp_own", has_content=False, credential="bp_cred")
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_browser_memory_engine_enabled", AsyncMock(return_value=True))
    updated = SimpleNamespace(workflow_run_id="wr_test")
    update_run = AsyncMock(return_value=updated)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run", update_run)
    pin = AsyncMock(return_value=updated)
    monkeypatch.setattr(svc, "_maybe_pin_credential_profile_ip", pin)

    workflow = _workflow(persist=True)
    result = await svc._resolve_and_stamp_run_seed(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run=_run(),  # type: ignore[arg-type]
        parameter_values={},
        explicit_request_browser_profile_id=None,
    )

    # Row 5 (save on + credential, own empty): seed the credential, sink the own auto-profile.
    assert result is updated
    update_run.assert_awaited_once_with(
        workflow_run_id="wr_test",
        browser_profile_id="bp_cred",
        browser_seed_source=BrowserSeedSource.credential,
        browser_sink_profile_id="bp_own",
    )
    pin.assert_awaited_once_with(
        workflow=workflow,
        workflow_run=updated,
        parameter_values={},
        seed_profile_id="bp_cred",
        organization_id="o_test",
    )


@pytest.mark.asyncio
async def test_resolve_and_stamp_skips_pin_when_engine_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # The credential IP-pin is a new runtime effect (a proxy change) gated on the engine kill-switch:
    # flag-off runs keep today's proxy behavior and never get the dedicated-IP headers.
    svc = _svc(monkeypatch, managed="bp_own", has_content=False, credential="bp_cred")
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_browser_memory_engine_enabled", AsyncMock(return_value=False))
    updated = SimpleNamespace(workflow_run_id="wr_test")
    update_run = AsyncMock(return_value=updated)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run", update_run)
    pin = AsyncMock(return_value=updated)
    monkeypatch.setattr(svc, "_maybe_pin_credential_profile_ip", pin)

    result = await svc._resolve_and_stamp_run_seed(
        workflow=_workflow(persist=True),  # type: ignore[arg-type]
        workflow_run=_run(),  # type: ignore[arg-type]
        parameter_values={},
        explicit_request_browser_profile_id=None,
    )

    assert result is updated
    pin.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_block_skips_credential_load_for_start_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # A start_fresh_browser run reads no saved memory by contract, so the mid-run login block must not
    # load the credential's profile. Holds flag-off too — the field is new, so no legacy run sets it.
    svc = WorkflowService()
    proxy_pin = AsyncMock()
    resolve_bpid = AsyncMock()
    monkeypatch.setattr(svc, "_apply_login_block_credential_proxy_pin", proxy_pin)
    monkeypatch.setattr(svc, "_resolve_login_block_browser_profile_id", resolve_bpid)
    run = SimpleNamespace(browser_seed_source=BrowserSeedSource.fresh, start_fresh_browser=True)

    result = await svc._prepare_login_block_browser_profile(
        block=SimpleNamespace(),  # type: ignore[arg-type]
        workflow_run=run,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        browser_session_id=None,
    )

    assert result is run
    proxy_pin.assert_not_awaited()
    resolve_bpid.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_block_override_shortcircuit_is_engine_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag-on: an explicit override wins and skips the login block's credential machinery.
    svc = WorkflowService()
    proxy_pin = AsyncMock()
    monkeypatch.setattr(svc, "_apply_login_block_credential_proxy_pin", proxy_pin)
    monkeypatch.setattr(svc, "_resolve_login_block_browser_profile_id", AsyncMock())
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_browser_memory_engine_enabled", AsyncMock(return_value=True))
    run = SimpleNamespace(browser_seed_source=BrowserSeedSource.override, start_fresh_browser=False)

    result = await svc._prepare_login_block_browser_profile(
        block=SimpleNamespace(),  # type: ignore[arg-type]
        workflow_run=run,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        browser_session_id=None,
    )

    assert result is run
    proxy_pin.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_block_override_runs_credential_path_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flag-off: the override short-circuit must NOT fire (byte-for-byte legacy), so the run reaches the
    # credential proxy-pin. A sentinel raise there proves we fell through instead of short-circuiting.
    svc = WorkflowService()
    monkeypatch.setattr(svc, "_apply_login_block_credential_proxy_pin", AsyncMock(side_effect=RuntimeError("reached")))
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_browser_memory_engine_enabled", AsyncMock(return_value=False))
    run = SimpleNamespace(browser_seed_source=BrowserSeedSource.override, start_fresh_browser=False)

    with pytest.raises(RuntimeError, match="reached"):
        await svc._prepare_login_block_browser_profile(
            block=SimpleNamespace(),  # type: ignore[arg-type]
            workflow_run=run,  # type: ignore[arg-type]
            workflow_run_id="wr_test",
            organization_id="o_test",
            browser_session_id=None,
        )


@pytest.mark.asyncio
async def test_resolve_and_stamp_is_idempotent_when_already_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _svc(monkeypatch, credential="bp_cred")
    update_run = AsyncMock()
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run", update_run)
    already = SimpleNamespace(
        workflow_run_id="wr_test",
        organization_id="o_test",
        browser_session_id=None,
        browser_profile_id="bp_cred",
        browser_seed_source=BrowserSeedSource.credential,
        browser_sink_profile_id=None,
        proxy_location=None,
    )

    result = await svc._resolve_and_stamp_run_seed(
        workflow=_workflow(),  # type: ignore[arg-type]
        workflow_run=already,  # type: ignore[arg-type]
        parameter_values={},
        explicit_request_browser_profile_id=None,
    )

    assert result is already
    update_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_and_stamp_keeps_session_attached_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    # A run bound to a live session keeps that session's propagated profile — no resolve, no clobber.
    svc = _svc(monkeypatch, credential="bp_cred")
    update_run = AsyncMock()
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run", update_run)
    session_run = SimpleNamespace(
        workflow_run_id="wr_test",
        organization_id="o_test",
        browser_session_id="pbs_1",
        browser_profile_id="bp_from_session",
        browser_seed_source=None,
        browser_sink_profile_id=None,
        proxy_location=None,
    )

    result = await svc._resolve_and_stamp_run_seed(
        workflow=_workflow(),  # type: ignore[arg-type]
        workflow_run=session_run,  # type: ignore[arg-type]
        parameter_values={},
        explicit_request_browser_profile_id=None,
    )

    assert result is session_run
    update_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_and_stamp_skips_profileless_live_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # A live session without a linked profile still governs the browser: don't stamp a credential/seed
    # that the running session would never load (false provenance).
    svc = _svc(monkeypatch, credential="bp_cred")
    update_run = AsyncMock()
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run", update_run)
    session_run = SimpleNamespace(
        workflow_run_id="wr_test",
        organization_id="o_test",
        browser_session_id="pbs_1",
        browser_profile_id=None,
        browser_seed_source=None,
        browser_sink_profile_id=None,
        proxy_location=None,
    )

    result = await svc._resolve_and_stamp_run_seed(
        workflow=_workflow(),  # type: ignore[arg-type]
        workflow_run=session_run,  # type: ignore[arg-type]
        parameter_values={},
        explicit_request_browser_profile_id=None,
    )

    assert result is session_run
    update_run.assert_not_awaited()


# --- credential IP pin: keep the same IP for sign-ins with a pinning credential ----


@pytest.mark.asyncio
async def test_maybe_pin_applies_headers_when_seed_is_pinning_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # The resolved seed is a credential's profile and that credential pins its IP -> apply its
    # dedicated-IP headers (covers explicit picks of a credential profile and rotation, not only
    # seed_source=credential).
    svc = WorkflowService()
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_credentials_by_browser_profile_id",
        AsyncMock(
            return_value=[SimpleNamespace(pin_saved_session_ip=True, proxy_session_id="ps_1", credential_id="c")]
        ),
    )
    updated = SimpleNamespace(workflow_run_id="wr_test")
    update_run = AsyncMock(return_value=updated)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run", update_run)
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_proxy_session_extra_http_headers", lambda headers: False)
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "merge_proxy_session_extra_http_headers", lambda headers, ps: {"x-sky-proxy": ps}
    )

    monkeypatch.setattr(svc, "_resolve_active_credential_pin_for_setup", AsyncMock(return_value=None))
    run = SimpleNamespace(workflow_run_id="wr_test", extra_http_headers=None)
    result = await svc._maybe_pin_credential_profile_ip(
        workflow=SimpleNamespace(),  # type: ignore[arg-type]
        workflow_run=run,  # type: ignore[arg-type]
        parameter_values={},
        seed_profile_id="bp_cred",
        organization_id="o_test",
    )

    assert result is updated
    kwargs = update_run.await_args.kwargs
    assert kwargs["extra_http_headers"] == {"x-sky-proxy": "ps_1"}
    assert kwargs["proxy_location"] == ProxyLocation.RESIDENTIAL_ISP


@pytest.mark.asyncio
async def test_maybe_pin_skips_when_owning_credential_does_not_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = WorkflowService()
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_credentials_by_browser_profile_id",
        AsyncMock(
            return_value=[SimpleNamespace(pin_saved_session_ip=False, proxy_session_id="ps_1", credential_id="c")]
        ),
    )
    update_run = AsyncMock()
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run", update_run)
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_proxy_session_extra_http_headers", lambda headers: False)

    monkeypatch.setattr(svc, "_resolve_active_credential_pin_for_setup", AsyncMock(return_value=None))
    run = SimpleNamespace(workflow_run_id="wr_test", extra_http_headers=None)
    result = await svc._maybe_pin_credential_profile_ip(
        workflow=SimpleNamespace(),  # type: ignore[arg-type]
        workflow_run=run,  # type: ignore[arg-type]
        parameter_values={},
        seed_profile_id="bp_cred",
        organization_id="o_test",
    )

    assert result is run
    update_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_pin_noop_without_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = WorkflowService()
    monkeypatch.setattr(svc, "_resolve_active_credential_pin_for_setup", AsyncMock(return_value=None))
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_proxy_session_extra_http_headers", lambda headers: False)
    run = SimpleNamespace(workflow_run_id="wr_test", extra_http_headers=None)
    result = await svc._maybe_pin_credential_profile_ip(
        workflow=SimpleNamespace(),  # type: ignore[arg-type]
        workflow_run=run,  # type: ignore[arg-type]
        parameter_values={},
        seed_profile_id=None,
        organization_id="o_test",
    )
    assert result is run


@pytest.mark.asyncio
async def test_maybe_pin_active_credential_wins_over_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    # B4: the active login credential's pin wins over the seed profile's own pin (site tracks IP per account).
    svc = WorkflowService()
    monkeypatch.setattr(
        svc, "_resolve_active_credential_pin_for_setup", AsyncMock(return_value=("c_active", "ps_active"))
    )
    seed_owners = AsyncMock(
        return_value=[SimpleNamespace(pin_saved_session_ip=True, proxy_session_id="ps_seed", credential_id="c_seed")]
    )
    monkeypatch.setattr(app.DATABASE.credentials, "get_credentials_by_browser_profile_id", seed_owners)
    updated = SimpleNamespace(workflow_run_id="wr_test")
    update_run = AsyncMock(return_value=updated)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run", update_run)
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_proxy_session_extra_http_headers", lambda headers: False)
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "merge_proxy_session_extra_http_headers", lambda headers, ps: {"x-sky-proxy": ps}
    )
    run = SimpleNamespace(workflow_run_id="wr_test", extra_http_headers=None)

    result = await svc._maybe_pin_credential_profile_ip(
        workflow=SimpleNamespace(),  # type: ignore[arg-type]
        workflow_run=run,  # type: ignore[arg-type]
        parameter_values={},
        seed_profile_id="bp_cred",
        organization_id="o_test",
    )

    assert result is updated
    assert update_run.await_args.kwargs["extra_http_headers"] == {"x-sky-proxy": "ps_active"}
    seed_owners.assert_not_awaited()  # active pin wins → the seed owner is never consulted


@pytest.mark.asyncio
async def test_maybe_pin_active_credential_applies_without_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    # B4: a pinned active credential applies its IP even when the run has no seed profile of its own.
    svc = WorkflowService()
    monkeypatch.setattr(
        svc, "_resolve_active_credential_pin_for_setup", AsyncMock(return_value=("c_active", "ps_active"))
    )
    updated = SimpleNamespace(workflow_run_id="wr_test")
    update_run = AsyncMock(return_value=updated)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run", update_run)
    monkeypatch.setattr(app.AGENT_FUNCTION, "has_proxy_session_extra_http_headers", lambda headers: False)
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "merge_proxy_session_extra_http_headers", lambda headers, ps: {"x-sky-proxy": ps}
    )
    run = SimpleNamespace(workflow_run_id="wr_test", extra_http_headers=None)
    result = await svc._maybe_pin_credential_profile_ip(
        workflow=SimpleNamespace(),  # type: ignore[arg-type]
        workflow_run=run,  # type: ignore[arg-type]
        parameter_values={},
        seed_profile_id=None,
        organization_id="o_test",
    )
    assert result is updated
    assert update_run.await_args.kwargs["extra_http_headers"] == {"x-sky-proxy": "ps_active"}


# --- credential parameter builder (shared) -----------------------------------


def _credential_parameter(
    key: str,
    credential_id: str,
    credential_ids: list[str] | None = None,
    fallback_credential_ids: list[str] | None = None,
) -> CredentialParameter:
    now = datetime.now(timezone.utc)
    return CredentialParameter(
        key=key,
        credential_parameter_id=f"cp_{key}",
        workflow_id="wf_test",
        credential_id=credential_id,
        credential_ids=credential_ids or [],
        fallback_credential_ids=fallback_credential_ids or [],
        created_at=now,
        modified_at=now,
    )


# --- retry re-resolves instead of pinning ------------------------------------


def _existing_run(
    *,
    browser_profile_id: str | None,
    browser_seed_source: BrowserSeedSource | None,
    start_fresh_browser: bool | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        proxy_location=None,
        webhook_callback_url=None,
        totp_verification_url=None,
        totp_identifier=None,
        browser_session_id=None,
        browser_profile_id=browser_profile_id,
        browser_seed_source=browser_seed_source,
        start_fresh_browser=start_fresh_browser,
        max_screenshot_scrolls=None,
        max_elapsed_time_minutes=None,
        extra_http_headers=None,
        cdp_connect_headers=None,
        browser_address=None,
        run_with="agent",
        ai_fallback=None,
    )


def test_retry_propagates_explicit_override() -> None:
    run = _existing_run(browser_profile_id="bp_over", browser_seed_source=BrowserSeedSource.override)
    body = workflow_request_body_from_existing_run(run)  # type: ignore[arg-type]
    assert body.browser_profile_id == "bp_over"


def test_retry_reresolves_runtime_stamped_profile() -> None:
    # A credential/own-memory profile stamped at setup must NOT pin the retry.
    run = _existing_run(browser_profile_id="bp_cred", browser_seed_source=BrowserSeedSource.credential)
    body = workflow_request_body_from_existing_run(run)  # type: ignore[arg-type]
    assert body.browser_profile_id is None


def test_retry_reresolves_picked_profile() -> None:
    # A run-time picked profile is stamped as provenance, not an original-request override — re-resolve.
    run = _existing_run(browser_profile_id="bp_pick", browser_seed_source=BrowserSeedSource.picked)
    body = workflow_request_body_from_existing_run(run)  # type: ignore[arg-type]
    assert body.browser_profile_id is None


def test_retry_legacy_null_source_keeps_propagating() -> None:
    # Pre-S rows (seed source unknown) keep today's propagate behavior — no regression.
    run = _existing_run(browser_profile_id="bp_legacy", browser_seed_source=None)
    body = workflow_request_body_from_existing_run(run)  # type: ignore[arg-type]
    assert body.browser_profile_id == "bp_legacy"


def test_retry_propagates_start_fresh_browser() -> None:
    # The original request's fresh-browser intent rides into the retry (SKY-12644).
    run = _existing_run(browser_profile_id=None, browser_seed_source=BrowserSeedSource.fresh, start_fresh_browser=True)
    body = workflow_request_body_from_existing_run(run)  # type: ignore[arg-type]
    assert body.start_fresh_browser is True


def test_retry_start_fresh_browser_defaults_false_for_legacy_rows() -> None:
    run = _existing_run(browser_profile_id="bp_legacy", browser_seed_source=None)
    body = workflow_request_body_from_existing_run(run)  # type: ignore[arg-type]
    assert body.start_fresh_browser is False


# --- setup credential resolution: single-login gate + best-effort ------------


def _output_parameter(key: str) -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(output_parameter_id=f"{key}_id", key=key, workflow_id="wf", created_at=now, modified_at=now)


def _login_block(
    label: str,
    credential_param: CredentialParameter | WorkflowParameter,
    *,
    url: str = "https://example.com/login",
) -> LoginBlock:
    return LoginBlock(
        url=url,
        label=label,
        title=label,
        navigation_goal="log in",
        output_parameter=_output_parameter(f"{label}_out"),
        parameters=[credential_param],
    )


def _string_parameter(key: str) -> WorkflowParameter:
    now = datetime.now(timezone.utc)
    return WorkflowParameter(
        key=key,
        workflow_parameter_id=f"wp_{key}",
        workflow_parameter_type=WorkflowParameterType.STRING,
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def _workflow_credential_parameter(key: str, default_value: str | None) -> WorkflowParameter:
    now = datetime.now(timezone.utc)
    return WorkflowParameter(
        key=key,
        workflow_parameter_id=f"wp_{key}",
        workflow_parameter_type=WorkflowParameterType.CREDENTIAL_ID,
        workflow_id="wf_test",
        default_value=default_value,
        created_at=now,
        modified_at=now,
    )


def _workflow_with_blocks(*blocks: LoginBlock) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_permanent_id="wpid_test",
        workflow_definition=SimpleNamespace(blocks=list(blocks)),
    )


@pytest.mark.asyncio
async def test_setup_credential_resolves_single_login_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "workflow_run_contexts", {})  # no run context at setup
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_credential",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_cred")),
    )
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_cred")),
    )
    workflow = _workflow_with_blocks(_login_block("login", _credential_parameter("login", "cred_1")))

    result = await WorkflowService()._resolve_credential_browser_profile_id_for_setup(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        parameter_values={},
    )

    assert result == "bp_cred"


@pytest.mark.asyncio
async def test_setup_credential_defers_when_multiple_login_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    get_credential = AsyncMock()
    monkeypatch.setattr(app.DATABASE.credentials, "get_credential", get_credential)
    workflow = _workflow_with_blocks(
        _login_block("a", _credential_parameter("a", "cred_a")),
        _login_block("b", _credential_parameter("b", "cred_b")),
    )

    result = await WorkflowService()._resolve_credential_browser_profile_id_for_setup(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        parameter_values={},
    )

    # Ambiguous which login block executes -> defer to the mid-run stamp, don't even look one up.
    assert result is None
    get_credential.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_credential_best_effort_on_profile_lookup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "workflow_run_contexts", {})  # no run context at setup
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_credential",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_cred")),
    )
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(side_effect=RuntimeError("transient repo failure")),
    )
    workflow = _workflow_with_blocks(_login_block("login", _credential_parameter("login", "cred_1")))

    # A transient failure degrades to a fresh seed instead of failing setup.
    result = await WorkflowService()._resolve_credential_browser_profile_id_for_setup(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        parameter_values={},
    )

    assert result is None


@pytest.mark.asyncio
async def test_setup_credential_resolves_pool_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    # The defect surface: a pool credential (credential_ids) is invisible to the old in-memory setup
    # extractor, so setup seeded fresh while the block resolved the pool selection. That split brain only
    # bites when a cached-script / pre-navigating block opens the browser before the login block, locking
    # the mid-run loader out (get_or_create returns the already-open context) — leaving setup as the only
    # loader that can seed. Setup must resolve the pool selection via the SAME rich path as the block.
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "workflow_run_contexts", {})
    # The run's rotation selection is already persisted (select_credential_for_run is idempotent per run).
    monkeypatch.setattr(
        app.DATABASE.workflow_run_credential_selections,
        "get_selection",
        AsyncMock(return_value="cred_pool"),
    )
    get_credential = AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_pool"))
    monkeypatch.setattr(app.DATABASE.credentials, "get_credential", get_credential)
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_pool")),
    )
    workflow = _workflow_with_blocks(
        _login_block("login", _credential_parameter("login", "cred_primary", credential_ids=["cred_a", "cred_pool"]))
    )

    result = await WorkflowService()._resolve_credential_browser_profile_id_for_setup(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        parameter_values={},
    )

    assert result == "bp_pool"
    assert get_credential.await_args.kwargs["credential_id"] == "cred_pool"  # pool selection, not the static id


@pytest.mark.asyncio
async def test_setup_credential_resolves_fallback_db_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fallback retry persists a DB credential selection that differs from the parameter's static id.
    # The old in-memory extractor returned the static (primary) id, seeding the wrong account's profile;
    # the block resolved the DB selection. Setup must read the same DB selection.
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "workflow_run_contexts", {})
    monkeypatch.setattr(
        app.DATABASE.workflow_run_credential_selections,
        "get_selection",
        AsyncMock(return_value="cred_fallback"),
    )
    get_credential = AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_fallback"))
    monkeypatch.setattr(app.DATABASE.credentials, "get_credential", get_credential)
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_fallback")),
    )
    workflow = _workflow_with_blocks(
        _login_block("login", _credential_parameter("login", "cred_primary", fallback_credential_ids=["cred_fallback"]))
    )

    result = await WorkflowService()._resolve_credential_browser_profile_id_for_setup(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        parameter_values={},
    )

    assert result == "bp_fallback"
    assert get_credential.await_args.kwargs["credential_id"] == "cred_fallback"  # DB selection wins over static


@pytest.mark.asyncio
async def test_setup_credential_uses_same_rich_resolver_as_block(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression pin: setup resolves the single login block's credential through the identical resolver
    # the mid-run stamp uses (_resolve_login_block_credential_ids), so the two can never disagree.
    svc = WorkflowService()
    rich = AsyncMock(return_value=["cred_rich"])
    monkeypatch.setattr(svc, "_resolve_login_block_credential_ids", rich)
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_credential",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_rich")),
    )
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_rich")),
    )
    workflow = _workflow_with_blocks(_login_block("login", _credential_parameter("login", "cred_static")))

    result = await svc._resolve_credential_browser_profile_id_for_setup(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        parameter_values={},
    )

    assert result == "bp_rich"
    rich.assert_awaited_once()
    assert rich.await_args.args[1:] == ("wr_test", "o_test", "wpid_test")


@pytest.mark.asyncio
async def test_setup_pin_resolves_pool_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    # The sibling pin resolver had the identical gap: a pool/DB-selected credential was invisible, so a
    # pinning credential's dedicated IP was not applied at setup. It now resolves via the same rich path.
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "workflow_run_contexts", {})
    monkeypatch.setattr(
        app.DATABASE.workflow_run_credential_selections,
        "get_selection",
        AsyncMock(return_value="cred_pool"),
    )
    monkeypatch.setattr(
        app.DATABASE.credentials,
        "get_credential",
        AsyncMock(
            return_value=SimpleNamespace(
                credential_id="cred_pool", pin_saved_session_ip=True, proxy_session_id="ps_pool"
            )
        ),
    )
    workflow = _workflow_with_blocks(
        _login_block("login", _credential_parameter("login", "cred_primary", credential_ids=["cred_a", "cred_pool"]))
    )

    result = await WorkflowService()._resolve_active_credential_pin_for_setup(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        parameter_values={},
    )

    assert result == ("cred_pool", "ps_pool")


@pytest.mark.asyncio
async def test_setup_credential_prefers_request_value_over_default_for_workflow_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Setup runs before run parameters are persisted, so a request-supplied WorkflowParameter/CREDENTIAL_ID
    # value lives only in the in-memory render params. Seeding must use it, not the parameter default, or a
    # cached-script run seeds the wrong account.
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "workflow_run_contexts", {})
    get_credential = AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_request"))
    monkeypatch.setattr(app.DATABASE.credentials, "get_credential", get_credential)
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_request")),
    )
    workflow = _workflow_with_blocks(
        _login_block("login", _workflow_credential_parameter("login_wp", "cred_default_a"))
    )

    result = await WorkflowService()._resolve_credential_browser_profile_id_for_setup(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        parameter_values={"login_wp": "cred_request_b"},
    )

    assert result == "bp_request"
    assert get_credential.await_args.kwargs["credential_id"] == "cred_request_b"  # request value, not the default


@pytest.mark.asyncio
async def test_setup_credential_resolves_dereferenced_binding_for_fallback_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fallback-configured CredentialParameter whose credential_id indirectly references another parameter
    # is dereferenced by the render pipeline into the in-memory params. With no persisted fallback selection
    # yet, the rich resolver would return the raw reference; setup must use the dereferenced value.
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "workflow_run_contexts", {})
    monkeypatch.setattr(app.DATABASE.workflow_run_credential_selections, "get_selection", AsyncMock(return_value=None))
    get_credential = AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_deref"))
    monkeypatch.setattr(app.DATABASE.credentials, "get_credential", get_credential)
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_browser_profile",
        AsyncMock(return_value=SimpleNamespace(browser_profile_id="bp_deref")),
    )
    workflow = _workflow_with_blocks(
        _login_block("login", _credential_parameter("login_cred", "raw_reference", fallback_credential_ids=["fb"]))
    )

    result = await WorkflowService()._resolve_credential_browser_profile_id_for_setup(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        parameter_values={"login_cred": "cred_deref_b"},
    )

    assert result == "bp_deref"
    assert get_credential.await_args.kwargs["credential_id"] == "cred_deref_b"  # dereferenced, not "raw_reference"


@pytest.mark.asyncio
async def test_setup_pin_prefers_request_value_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # The pin resolver shares the setup credential resolution, so a request-supplied credential's pin must
    # win over the parameter default's pin.
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "workflow_run_contexts", {})
    get_credential = AsyncMock(
        return_value=SimpleNamespace(credential_id="cred_b", pin_saved_session_ip=True, proxy_session_id="ps_b")
    )
    monkeypatch.setattr(app.DATABASE.credentials, "get_credential", get_credential)
    workflow = _workflow_with_blocks(
        _login_block("login", _workflow_credential_parameter("login_wp", "cred_default_a"))
    )

    result = await WorkflowService()._resolve_active_credential_pin_for_setup(
        workflow=workflow,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        parameter_values={"login_wp": "cred_b"},
    )

    assert result == ("cred_b", "ps_b")
    assert get_credential.await_args.kwargs["credential_id"] == "cred_b"  # request value, not the default


@pytest.mark.asyncio
async def test_login_block_cached_browser_degrades_instead_of_credential_seeding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A prior block already opened this run's browser: get_or_create_for_workflow_run would return that
    # cached context and ignore the credential profile, so stamping the run credential-seeded would let the
    # healthy-run bank whole-dir the unrelated context into the SHARED credential profile. Degrade to
    # degraded_fresh (profile None) and do not attempt a load instead.
    svc = WorkflowService()
    monkeypatch.setattr(svc, "_apply_login_block_credential_proxy_pin", AsyncMock())
    monkeypatch.setattr(svc, "_resolve_login_block_browser_profile_id", AsyncMock(return_value="cred_profile_x"))
    monkeypatch.setattr(
        svc,
        "_evaluate_debug_session_profile_decision",
        AsyncMock(return_value=SimpleNamespace(incompatible_reason=None, attach_browser_session_id=None)),
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_browser_memory_engine_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", lambda *a, **k: object())  # browser exists
    get_or_create = AsyncMock()
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_or_create_for_workflow_run", get_or_create)
    update = AsyncMock()
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", update)
    # The reload reflects the DB's post-update (degraded_fresh) state.
    reloaded = SimpleNamespace(
        browser_seed_source=BrowserSeedSource.degraded_fresh,
        browser_profile_id=None,
        start_fresh_browser=False,
        workflow_permanent_id="wpid",
    )
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=reloaded))

    block = SimpleNamespace(navigation_goal="log in", url="https://site.example/home", label="login")
    run = SimpleNamespace(
        browser_seed_source=BrowserSeedSource.credential, start_fresh_browser=False, workflow_permanent_id="wpid"
    )
    result = await svc._prepare_login_block_browser_profile(
        block=block,  # type: ignore[arg-type]
        workflow_run=run,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        browser_session_id=None,
    )

    update.assert_awaited_once_with(
        workflow_run_id="wr_test", browser_profile_id=None, browser_seed_source=BrowserSeedSource.degraded_fresh
    )
    get_or_create.assert_not_awaited()  # never loads a profile into the cached context
    # Regression: the RETURNED object must reflect degraded_fresh (not the stale "credential"), or the
    # downstream healthy-run bank reads this object and banks the unrelated context into the shared profile.
    assert result.browser_seed_source == BrowserSeedSource.degraded_fresh
    assert result.browser_profile_id is None


@pytest.mark.asyncio
async def test_login_block_no_cached_browser_still_credential_seeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Control: with no browser open yet (the common single-login case), the mid-run stamp still records
    # credential provenance — the cached-browser guard must not over-fire.
    svc = WorkflowService()
    monkeypatch.setattr(svc, "_apply_login_block_credential_proxy_pin", AsyncMock())
    monkeypatch.setattr(svc, "_resolve_login_block_browser_profile_id", AsyncMock(return_value="cred_profile_x"))
    monkeypatch.setattr(
        svc,
        "_evaluate_debug_session_profile_decision",
        AsyncMock(return_value=SimpleNamespace(incompatible_reason=None, attach_browser_session_id=None)),
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_browser_memory_engine_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", lambda *a, **k: None)  # no browser yet
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", (update := AsyncMock()))
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=None))

    block = SimpleNamespace(navigation_goal="log in", url=None, label="login")  # no url -> stamp only, no load
    run = SimpleNamespace(
        browser_seed_source=BrowserSeedSource.credential, start_fresh_browser=False, workflow_permanent_id="wpid"
    )
    await svc._prepare_login_block_browser_profile(
        block=block,  # type: ignore[arg-type]
        workflow_run=run,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        browser_session_id=None,
    )

    update.assert_awaited_once_with(
        workflow_run_id="wr_test",
        browser_profile_id="cred_profile_x",
        browser_seed_source=BrowserSeedSource.credential,
    )


async def _prepare_login_block_profile_boot(
    monkeypatch: pytest.MonkeyPatch,
    *,
    url: str,
    values: dict[str, str],
    get_or_create: AsyncMock,
    get_workflow_run: AsyncMock | None = None,
) -> tuple[object, AsyncMock, LoginBlock]:
    context = WorkflowRunContext(
        workflow_title="Workflow",
        workflow_id="wf_test",
        workflow_permanent_id="wpid",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
    )
    context.values.update(values)
    for key in values:
        context.parameters[key] = _string_parameter(key)
    # The stub app auto-mocks WORKFLOW_CONTEXT_MANAGER attributes as AsyncMocks, so the real manager is
    # installed here to exercise the actual context lookup the renderer does.
    context_manager = WorkflowContextManager()
    context_manager.workflow_run_contexts["wr_test"] = context
    monkeypatch.setattr(app, "WORKFLOW_CONTEXT_MANAGER", context_manager)

    svc = WorkflowService()
    monkeypatch.setattr(svc, "_apply_login_block_credential_proxy_pin", AsyncMock())
    monkeypatch.setattr(svc, "_resolve_login_block_browser_profile_id", AsyncMock(return_value="cred_profile_x"))
    monkeypatch.setattr(
        svc,
        "_evaluate_debug_session_profile_decision",
        AsyncMock(return_value=SimpleNamespace(incompatible_reason=None, attach_browser_session_id=None)),
    )
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_for_workflow_run", lambda *a, **k: None)
    monkeypatch.setattr(app.BROWSER_MANAGER, "get_or_create_for_workflow_run", get_or_create)
    update = AsyncMock()
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", update)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run",
        get_workflow_run or AsyncMock(return_value=None),
    )

    block = _login_block("login", _credential_parameter("login", "cred_1"), url=url)
    run = SimpleNamespace(
        browser_seed_source=BrowserSeedSource.credential,
        start_fresh_browser=False,
        workflow_permanent_id="wpid",
    )
    result = await svc._prepare_login_block_browser_profile(
        block=block,
        workflow_run=run,  # type: ignore[arg-type]
        workflow_run_id="wr_test",
        organization_id="o_test",
        browser_session_id=None,
    )
    return result, update, block


@pytest.mark.parametrize(
    ("raw_url", "values", "expected_url"),
    [
        pytest.param(
            "{{ some_url_parameter }}",
            {"some_url_parameter": "login.example/session"},
            "https://login.example/session",
            id="templated",
        ),
        pytest.param(
            "https://login.example/session",
            {},
            "https://login.example/session",
            id="literal",
        ),
        pytest.param(
            "some_url_parameter",
            {"some_url_parameter": "https://login.example/session"},
            "https://login.example/session",
            id="direct_parameter_key",
        ),
        pytest.param(
            # Pins the resolution ORDER: the direct key must be substituted BEFORE jinja runs, or the
            # nested template survives unrendered.
            "url_param",
            {"url_param": "{{ host }}/login", "host": "login.example"},
            "https://login.example/login",
            id="direct_parameter_key_holding_a_template",
        ),
        pytest.param(
            "login.example/session",
            {},
            "https://login.example/session",
            id="schemeless_literal_is_normalized",
        ),
        pytest.param(
            "https://www.www.login.example/session",
            {},
            "https://www.www.login.example/session",
            id="schemed_literal_is_passed_through_uncanonicalized",
        ),
        pytest.param(
            "not a url",
            {},
            "not a url",
            id="schemeless_literal_that_fails_validation_keeps_raw",
        ),
        pytest.param(
            "{{ some_url_parameter }}",
            {"some_url_parameter": "not a url"},
            "{{ some_url_parameter }}",
            id="renders_to_invalid_url_keeps_raw",
        ),
    ],
)
@pytest.mark.asyncio
async def test_login_block_profile_boot_resolves_url(
    monkeypatch: pytest.MonkeyPatch,
    raw_url: str,
    values: dict[str, str],
    expected_url: str,
) -> None:
    page = SimpleNamespace(url=expected_url, wait_for_load_state=AsyncMock())
    get_or_create = AsyncMock(return_value=SimpleNamespace(get_working_page=AsyncMock(return_value=page)))

    _, _, block = await _prepare_login_block_profile_boot(
        monkeypatch,
        url=raw_url,
        values=values,
        get_or_create=get_or_create,
    )

    assert get_or_create.await_args.kwargs["url"] == expected_url
    assert block.url == raw_url


@pytest.mark.asyncio
async def test_login_block_profile_boot_unresolved_url_degrades_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_or_create = AsyncMock(side_effect=RuntimeError("profile boot failed"))
    degraded_run = SimpleNamespace(
        browser_seed_source=BrowserSeedSource.degraded_fresh,
        browser_profile_id=None,
    )

    raw_url = "{{ missing_url_parameter }}"
    result, update, _ = await _prepare_login_block_profile_boot(
        monkeypatch,
        url=raw_url,
        values={},
        get_or_create=get_or_create,
        get_workflow_run=AsyncMock(side_effect=[None, degraded_run]),
    )

    assert get_or_create.await_args.kwargs["url"] == raw_url
    assert update.await_args_list[-1].kwargs == {
        "workflow_run_id": "wr_test",
        "browser_profile_id": None,
        "browser_seed_source": BrowserSeedSource.degraded_fresh,
    }
    assert result is degraded_run
