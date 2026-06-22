"""Asymmetric debug-session vs workflow-run semantics for LoginBlock credential profile.

Debug-session runs (workflow_run.debug_session_id is not None) prioritize stream
fidelity: attach the visible PBS, and keep the skip-login fast path only when the
PBS profile actually matches the credential. Mismatched / missing PBS profile
yields a structured warning, no profile_id write, no navigation_goal rewrite, and
fall-through to ordinary LoginBlock execution so the user watches the actual
login. Non-debug runs preserve existing behavior — credential-profile launches a
fresh browser, no PBS attach. The decision lives in
``WorkflowService._evaluate_debug_session_profile_decision``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.workflow.service import WorkflowService


def _workflow_run(*, debug_session_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_run_id="wr_test",
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
        debug_session_id=debug_session_id,
        browser_session_id="pbs_test" if debug_session_id else None,
        browser_profile_id=None,
        parent_workflow_run_id=None,
    )


def _pbs(*, browser_profile_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        persistent_browser_session_id="pbs_test",
        organization_id="o_test",
        browser_profile_id=browser_profile_id,
    )


class TestDebugSessionProfileDecision:
    """The helper returns the asymmetric decision struct used downstream."""

    @pytest.mark.asyncio
    async def test_non_debug_run_keeps_existing_skip_login_no_pbs_attach(self) -> None:
        service = WorkflowService()
        workflow_run = _workflow_run(debug_session_id=None)

        with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
            mock_app.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
                return_value=_pbs(browser_profile_id="bp_cred"),
            )
            decision = await service._evaluate_debug_session_profile_decision(
                workflow_run=workflow_run,
                browser_session_id=None,
                resolved_browser_profile_id="bp_cred",
                organization_id="o_test",
            )
            # Non-debug runs must short-circuit before touching the PBS lookup;
            # the mock is wired only to prove that contract is preserved.
            mock_app.DATABASE.browser_sessions.get_persistent_browser_session.assert_not_called()

        assert decision.attach_browser_session_id is None
        assert decision.incompatible_reason is None

    @pytest.mark.asyncio
    async def test_debug_run_no_browser_session_id_keeps_existing_behavior(self) -> None:
        """Debug-run flag set but no PBS attached — preserve existing flow."""
        service = WorkflowService()
        workflow_run = _workflow_run(debug_session_id="ds_test")

        with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
            mock_app.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
                return_value=None,
            )
            decision = await service._evaluate_debug_session_profile_decision(
                workflow_run=workflow_run,
                browser_session_id=None,
                resolved_browser_profile_id="bp_cred",
                organization_id="o_test",
            )

        assert decision.attach_browser_session_id is None
        assert decision.incompatible_reason is None

    @pytest.mark.asyncio
    async def test_debug_run_compatible_profile_threads_pbs_keeps_skip_login(self) -> None:
        service = WorkflowService()
        workflow_run = _workflow_run(debug_session_id="ds_test")

        with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
            mock_app.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
                return_value=_pbs(browser_profile_id="bp_cred"),
            )
            decision = await service._evaluate_debug_session_profile_decision(
                workflow_run=workflow_run,
                browser_session_id="pbs_test",
                resolved_browser_profile_id="bp_cred",
                organization_id="o_test",
            )

        assert decision.attach_browser_session_id == "pbs_test"
        assert decision.incompatible_reason is None

    @pytest.mark.asyncio
    async def test_debug_run_pbs_no_profile_is_incompatible(self) -> None:
        service = WorkflowService()
        workflow_run = _workflow_run(debug_session_id="ds_test")

        with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
            mock_app.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
                return_value=_pbs(browser_profile_id=None),
            )
            decision = await service._evaluate_debug_session_profile_decision(
                workflow_run=workflow_run,
                browser_session_id="pbs_test",
                resolved_browser_profile_id="bp_cred",
                organization_id="o_test",
            )

        assert decision.attach_browser_session_id == "pbs_test"
        assert decision.incompatible_reason == "pbs_no_profile"

    @pytest.mark.asyncio
    async def test_debug_run_pbs_different_profile_is_incompatible(self) -> None:
        service = WorkflowService()
        workflow_run = _workflow_run(debug_session_id="ds_test")

        with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
            mock_app.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
                return_value=_pbs(browser_profile_id="bp_other"),
            )
            decision = await service._evaluate_debug_session_profile_decision(
                workflow_run=workflow_run,
                browser_session_id="pbs_test",
                resolved_browser_profile_id="bp_cred",
                organization_id="o_test",
            )

        assert decision.attach_browser_session_id == "pbs_test"
        assert decision.incompatible_reason == "pbs_different_profile"

    @pytest.mark.asyncio
    async def test_debug_run_pbs_lookup_missing_is_pbs_no_profile(self) -> None:
        """If the PBS row can't be fetched (race / orphan), treat it as no-profile.

        The visible browser still exists from the user's perspective; we attach it
        and fall through to manual login rather than silently inheriting the
        credential profile into a foreign browser.
        """
        service = WorkflowService()
        workflow_run = _workflow_run(debug_session_id="ds_test")

        with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
            mock_app.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
                return_value=None,
            )
            decision = await service._evaluate_debug_session_profile_decision(
                workflow_run=workflow_run,
                browser_session_id="pbs_test",
                resolved_browser_profile_id="bp_cred",
                organization_id="o_test",
            )

        assert decision.attach_browser_session_id == "pbs_test"
        assert decision.incompatible_reason == "pbs_no_profile"

    @pytest.mark.asyncio
    async def test_debug_run_pbs_lookup_raises_is_treated_as_pbs_no_profile(self) -> None:
        """A transient DB/PBS lookup exception must not fail the workflow run.

        Mirror ``_hydrate_pbs_browser_profile_id`` in ``debug_sessions.py``: log a
        warning and treat the PBS profile as None so the decision falls into the
        ``pbs_no_profile`` incompatible branch — fail-safe attach + manual login.
        """
        service = WorkflowService()
        workflow_run = _workflow_run(debug_session_id="ds_test")

        with patch("skyvern.forge.sdk.workflow.service.app") as mock_app:
            mock_app.DATABASE.browser_sessions.get_persistent_browser_session = AsyncMock(
                side_effect=RuntimeError("transient db failure"),
            )
            decision = await service._evaluate_debug_session_profile_decision(
                workflow_run=workflow_run,
                browser_session_id="pbs_test",
                resolved_browser_profile_id="bp_cred",
                organization_id="o_test",
            )

        assert decision.attach_browser_session_id == "pbs_test"
        assert decision.incompatible_reason == "pbs_no_profile"


class TestDebugSessionProfileWarningCode:
    """The structured warning code/reason emitted on incompatibility is part of the contract.

    Downstream observability dashboards key on `code=debug_session_profile_incompatible`
    plus `reason in {pbs_no_profile, pbs_different_profile}`. Locking the values in a test
    catches accidental rename / spelling drift.
    """

    def test_warning_code_constant_is_stable(self) -> None:
        from skyvern.forge.sdk.workflow.service import (
            DEBUG_SESSION_PROFILE_INCOMPATIBLE_CODE,
            DEBUG_SESSION_PROFILE_REASON_DIFFERENT,
            DEBUG_SESSION_PROFILE_REASON_NO_PROFILE,
        )

        assert DEBUG_SESSION_PROFILE_INCOMPATIBLE_CODE == "debug_session_profile_incompatible"
        assert DEBUG_SESSION_PROFILE_REASON_NO_PROFILE == "pbs_no_profile"
        assert DEBUG_SESSION_PROFILE_REASON_DIFFERENT == "pbs_different_profile"
