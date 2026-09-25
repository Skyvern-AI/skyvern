"""Fakes for the WORKFLOW_TASK_V3_AB engine A/B resolver, shared by the tests that drive it.

The resolver is exercised from two files -- the A/B's own tests and the workflow-run duration-log
tests, which read what the resolver pinned -- so the provider fake and the resolver driver live here
rather than being imported out of one test module's privates or pasted into the other.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import WorkflowRunTriggerType
from skyvern.forge.sdk.experimentation.billing_tier import BillingTier
from skyvern.forge.sdk.experimentation.providers import BaseExperimentationProvider
from skyvern.forge.sdk.experimentation.workflow_block_engine import (
    TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG,
    resolve_workflow_block_engine_arm,
)
from skyvern.forge.sdk.workflow.models.block import V3AbIneligibleReason
from skyvern.schemas.workflows import WorkflowStatus
from tests.unit.test_agent_task_v3 import stub_workflow_block_engine_app

WORKFLOW_BLOCK_ENGINE_APP_TARGET = "skyvern.forge.sdk.experimentation.workflow_block_engine.app"


class FakeExperimentationProvider(BaseExperimentationProvider):
    """Shaped like the two cloud providers, which is what makes a direction-sensitive flag testable.

    In production an evaluation error never escapes ``_is_feature_enabled``: both PostHog providers
    swallow it into ``None`` and ``bool()`` it to ``False``, so only ``_resolve_feature_flag_strict``
    can tell a failure from a real ``False``. A fake that raised from ``_is_feature_enabled`` would
    verify a shape no provider has, and would let a fail-safe that is inverted in production pass.

    ``False`` here is the answer an INACTIVE flag gives: posthog's local evaluator returns a
    conclusive ``False`` for one before it reads any filter, so "disable the flag" and "roll it to 0%"
    arrive as the same value, and a rule that fired on ``False`` would enrol everybody the moment an
    operator switched the flag off.
    """

    def __init__(
        self,
        flags: dict[str, bool] | None = None,
        raise_error: bool = False,
        strict_error_flags: set[str] | None = None,
        unresolvable_flags: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.flags = dict(flags or {})
        # The rule fires only on a conclusive True, so a fake that did not mention this flag would
        # answer "undefined" and quietly take every enrolment test in this file off the rule and onto
        # the A/B path, where control and an unenrolled run look identical. Tests that want the other
        # resolutions say so, with a False here or with strict_error_flags/unresolvable_flags.
        self.flags.setdefault(TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG, True)
        self.calls: list[tuple[str, str, dict | None]] = []
        self.raise_error = raise_error
        self.strict_error_flags = set(strict_error_flags or ())
        # A flag key local evaluation cannot answer: absent from the snapshot the poller wrote, or
        # carrying a condition only the PostHog API can resolve. Neither resolver raises; both
        # return None.
        self.unresolvable_flags = set(unresolvable_flags or ())

    async def _prepare_feature_flag_resolution(self, feature_name: str, *, cached: bool) -> None:
        # Where a provider does raise in production: the local provider reloads its flag snapshot
        # from the database here, and a database failure escapes instead of resolving to a value.
        if self.raise_error:
            raise RuntimeError("provider unavailable")

    async def _evaluate(self, feature_name: str, distinct_id: str, properties: dict | None) -> bool | None:
        self.calls.append((feature_name, distinct_id, properties))
        if feature_name in self.strict_error_flags:
            raise RuntimeError("provider unavailable")
        if feature_name in self.unresolvable_flags:
            return None
        return self.flags.get(feature_name)

    async def _resolve_feature_flag_strict(
        self, feature_name: str, distinct_id: str, properties: dict | None = None
    ) -> bool | None:
        return await self._evaluate(feature_name, distinct_id, properties)

    async def _resolve_feature_flag(
        self, feature_name: str, distinct_id: str, properties: dict | None = None
    ) -> bool | None:
        try:
            return await self._evaluate(feature_name, distinct_id, properties)
        except Exception:
            return None

    async def _is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        return bool(await self._resolve_feature_flag(feature_name, distinct_id, properties))

    async def _get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        return None

    async def _get_payload(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> Any:
        return None


class Resolution(NamedTuple):
    log: dict[str, Any]
    birth_reads: AsyncMock
    warnings: list[Any]


def consulted_flags(provider: FakeExperimentationProvider) -> list[str]:
    return [flag for flag, _distinct_id, _properties in provider.calls]


# When the LATEST version of every workflow these helpers serve was saved, deliberately after the
# callers' v3-default cutoff: that models an old workflow edited after the cutoff, so a resolver
# reading the version this run executes instead of the permanent id's birth flips the arm and is
# caught behaviorally rather than by an assertion about a mock's call kwargs.
LATEST_VERSION_CREATED_AT = datetime(2026, 9, 20)


async def resolve_arm(
    context: SkyvernContext,
    provider: BaseExperimentationProvider,
    *,
    workflow_run_id: str,
    ineligibility_reason: V3AbIneligibleReason | None,
    organization_id: str | None = "org_1",
    workflow_permanent_id: str | None = "wpid_1",
    billing_tier: BillingTier = BillingTier.UNKNOWN,
    first_version_created_at: datetime | None = None,
    first_version_status: WorkflowStatus = WorkflowStatus.published,
    first_version_error: Exception | None = None,
    workflow_status: WorkflowStatus = WorkflowStatus.published,
    trigger_type: WorkflowRunTriggerType | None = WorkflowRunTriggerType.api,
) -> Resolution:
    async def read_birth_timestamp(workflow_permanent_id: str, organization_id: str) -> datetime | None:
        if first_version_error is not None:
            raise first_version_error
        return first_version_created_at

    async def read_workflow_version(
        workflow_permanent_id: str, *, version: int | None = None, **_: Any
    ) -> SimpleNamespace | None:
        if version != 1:
            return SimpleNamespace(created_at=LATEST_VERSION_CREATED_AT, status=WorkflowStatus.published)
        if first_version_created_at is None:
            return None
        return SimpleNamespace(created_at=first_version_created_at, status=first_version_status)

    birth_reads = AsyncMock(side_effect=read_birth_timestamp)
    with (
        patch(WORKFLOW_BLOCK_ENGINE_APP_TARGET) as mock_app,
        patch("skyvern.forge.sdk.experimentation.workflow_block_engine.LOG") as mock_log,
    ):
        mock_app.EXPERIMENTATION_PROVIDER = provider
        stub_workflow_block_engine_app(mock_app, billing_tier=billing_tier)
        # spec'd to the one repository the rule may reach, and to the two reads it could plausibly
        # use: on a bare MagicMock any attribute path answers, so a resolver reading a method the
        # real AgentDB does not have would pass here and raise in production, where the helper's
        # catch-all would bury it as "not a new workflow".
        database = MagicMock(spec=["workflows"])
        database.workflows = MagicMock(spec=["get_workflow_permanent_id_created_at", "get_workflow_by_permanent_id"])
        database.workflows.get_workflow_permanent_id_created_at = birth_reads
        # Answers whatever version is asked for, carrying the birth version's own status at version 1:
        # a resolver that regressed to deciding the per-call exclusion on the BIRTH version's status
        # reds the prompt-created case, and one that read the executing version's timestamp reds the
        # long-lived-workflow case, both behaviorally.
        database.workflows.get_workflow_by_permanent_id = AsyncMock(side_effect=read_workflow_version)
        mock_app.DATABASE = database
        await resolve_workflow_block_engine_arm(
            context,
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            workflow_status=workflow_status,
            trigger_type=trigger_type,
            ineligibility_reason=ineligibility_reason,
        )
    logged = dict(mock_log.info.call_args.kwargs) if mock_log.info.call_args else {}
    return Resolution(log=logged, birth_reads=birth_reads, warnings=list(mock_log.warning.call_args_list))
