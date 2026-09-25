from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.enums import JOB_RECIPE_WORKFLOW_RUN_TRIGGER_TYPES, WorkflowRunTriggerType
from skyvern.forge.sdk.experimentation.billing_tier import BILLING_TIER_PROPERTY, BillingTier
from skyvern.forge.sdk.experimentation.providers import BaseExperimentationProvider, NoOpExperimentationProvider
from skyvern.schemas.run_enums import RunEngine
from skyvern.schemas.workflows import WorkflowStatus

if TYPE_CHECKING:
    # Runtime import would cycle: block.py imports workflow_block_engine_override from this module.
    from skyvern.forge.sdk.workflow.models.block import V3AbIneligibleReason

LOG = structlog.get_logger()

WORKFLOW_TASK_V3_AB_FLAG = "WORKFLOW_TASK_V3_AB"
DISABLE_TASK_V3_FLAG = "DISABLE_TASK_V3"
TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG = "TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT"

# Trigger kinds whose run executes a workflow the platform minted for that one request rather than
# one a customer built. The job-recipe endpoints are the whole set today: each call creates a fresh
# workflow_permanent_id, runs it immediately, and never runs it again, so a per-workflow rule cannot
# match one against a control run of the same workflow.
PER_CALL_WORKFLOW_RUN_TRIGGER_TYPES: frozenset[WorkflowRunTriggerType] = JOB_RECIPE_WORKFLOW_RUN_TRIGGER_TYPES


class WorkflowBlockEngineRouteReason(StrEnum):
    """Why a workflow run landed on its arm, mirroring the bare-task RunRouteReason.

    Both arms persist the same engine on a control run, so without this the log cannot separate a
    bucketed control run from one that was never randomized -- and
    ``new_self_serve_workflow_default`` runs were never randomized, so every per-arm read has to
    exclude them by this field rather than by ``arm``. ``flag_bucket_control`` is therefore the
    randomized control cell and nothing else: a run the A/B flag never answered for is
    ``flag_undefined`` and a run whose evaluation raised is ``flag_error``, both on control.
    """

    flag_bucket_treatment = "flag_bucket_treatment"
    flag_bucket_control = "flag_bucket_control"
    new_self_serve_workflow_default = "new_self_serve_workflow_default"
    ineligible = "ineligible"
    disabled = "disabled"
    flag_undefined = "flag_undefined"
    flag_error = "flag_error"


class NewWorkflowDefaultRollout(StrEnum):
    """How ``TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT`` answered for a run the v3 default would enrol.

    Only ``enrolled`` -- a conclusive True -- lets the rule fire. ``not_enrolled`` is a conclusive
    False: the flag is inactive, the run fell outside the percentage, or a condition excluded it.
    ``undefined`` means the key never resolved at all: nobody created it, or local evaluation has no
    snapshot row for it yet. ``error`` means the evaluation raised. The last three are logged apart
    from each other because only ``not_enrolled`` is a randomized cell -- a read that wants this
    rule's control must pin that value, not the boolean.
    """

    enrolled = "enrolled"
    not_enrolled = "not_enrolled"
    undefined = "undefined"
    error = "error"


# Every resolution but a conclusive True leaves the run on the A/B path, so every off-state of the
# flag -- inactive, deleted, 0%, a condition that excluded the run, an evaluation that raised -- turns
# the rule off. An inactive flag is the one that has to be safe: posthog's local evaluator answers a
# conclusive False for it (``_compute_flag_locally``, "if not active: return False"), which is the
# gesture an operator reaches for mid-incident. Derived from the enum so a resolution added later
# leaves the run on the A/B instead of enrolling it.
_NOT_ENROLLED_RESOLUTIONS = frozenset(NewWorkflowDefaultRollout) - {NewWorkflowDefaultRollout.enrolled}


def _rollout_enrols(rollout: NewWorkflowDefaultRollout | None) -> bool:
    return rollout is not None and rollout not in _NOT_ENROLLED_RESOLUTIONS


def _arm_label(override: RunEngine | None) -> str:
    """The arm both the routing line and the duration log report, derived once from the engine the run
    routes on: only an explicit v3 override is treatment, so a future non-v3 override on that field
    cannot pass a truthiness check and read as one on one surface but not the other.
    """
    return "treatment" if override == RunEngine.skyvern_v3 else "control"


def engine_arm_log_value(value: StrEnum | None) -> str | None:
    """Enum facts about an arm are logged as their string value on both the arm-resolution line and
    the run's duration log, so one fact cannot be spelled two ways across the two surfaces.
    """
    return value.value if value is not None else None


@dataclass(frozen=True)
class WorkflowBlockEngineArmDecision:
    """The routing facts ``resolve_workflow_block_engine_arm`` decided for one workflow run.

    Pinned on that run's context beside the engine override, so finalize-time telemetry reports the
    values the experiment bucketed on rather than reading them again and possibly disagreeing with
    the routing line. A field is None when the resolution never got that far: ``billing_tier`` on an
    ineligible or killed run -- the same meaning it carries on the arm-resolution log, where it never
    doubles as "nobody looked" -- and every field when no experimentation provider is configured.
    """

    route_reason: WorkflowBlockEngineRouteReason | None = None
    billing_tier: BillingTier | None = None
    new_workflow_default_rollout_resolution: NewWorkflowDefaultRollout | None = None


NO_ARM_DECISION = WorkflowBlockEngineArmDecision()


@dataclass(frozen=True)
class WorkflowBlockEngineArmAttribution:
    """What a finalizer can say about a workflow run's engine arm, decision facts included.

    ``arm`` keeps the three-way contract from SKY-15561: "treatment"/"control" when this run's own
    context resolved an arm, None when that context never resolved one (the run never entered the
    A/B), "unknown" when attribution is lost -- a different context, or none, is current, which is
    the shape every out-of-band finalizer has (API cancel, the stuck-run sweep, copilot cooperative
    cancel). ``decision`` carries the facts pinned with that arm, and is the empty decision whenever
    there is none to report, so an attribution-lost read reports ``billing_tier`` UNKNOWN beside
    ``arm`` "unknown" rather than a tier nobody observed.
    """

    arm: str | None
    decision: WorkflowBlockEngineArmDecision = NO_ARM_DECISION


# Attribution lost, not "control with no tier": collapsing the two biases every per-arm read against
# the canceled and timed-out population, which is where out-of-band finalization concentrates.
ARM_ATTRIBUTION_LOST = WorkflowBlockEngineArmAttribution(
    arm="unknown",
    decision=WorkflowBlockEngineArmDecision(billing_tier=BillingTier.UNKNOWN),
)


async def task_v3_disabled(distinct_id: str, organization_id: str | None) -> bool:
    """The Task V3 kill switch, evaluated identically for the dispatch gate and the A/B resolver.

    The provider caches on (flag, distinct_id, properties), so both callers must build the same
    key or the kill switch can answer differently for the same run.
    """
    return await app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
        DISABLE_TASK_V3_FLAG, distinct_id, properties={"organization_id": organization_id}
    )


async def _billing_tier_for_arm(organization_id: str | None) -> BillingTier:
    """Never raises: a targeting property must not be able to decide the arm.

    The resolver's own catch-all turns any exception into control, so letting a tier read escape
    would let a billing-lookup failure silently move every run of the experiment onto v1 -- an
    outage in a system the arm never depended on before.
    """
    try:
        return await app.AGENT_FUNCTION.resolve_billing_tier(organization_id)
    except Exception:
        LOG.warning("Failed to resolve the billing tier for the engine arm", exc_info=True)
        return BillingTier.UNKNOWN


def _as_utc(value: datetime) -> datetime:
    # workflows.created_at is written by datetime.utcnow(), i.e. naive UTC, while the cutoff setting
    # arrives aware or naive depending on how it was spelled in the environment.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _workflow_is_new_for_v3_default(workflow_permanent_id: str | None, organization_id: str | None) -> bool:
    """Whether this permanent id was born at or after the v3-default cutoff.

    Never raises and never answers True on a read it could not complete: the rule can only add
    treatment, so any doubt has to leave the run on the arm the A/B would have given it.
    """
    cutoff = settings.TASK_V3_DEFAULT_ENGINE_WORKFLOW_CUTOFF
    if cutoff is None or not workflow_permanent_id or not organization_id:
        return False
    try:
        # The earliest version's timestamp, deleted versions included, is when the permanent id was
        # born. Reading the version this run executes would instead enrol every long-lived workflow
        # saved once after the cutoff.
        born_at = await app.DATABASE.workflows.get_workflow_permanent_id_created_at(
            workflow_permanent_id, organization_id
        )
        if born_at is None:
            return False
        # Inside the try with the read: a rule that raised here would reach the resolver's catch-all
        # and send the run to control, which is a different arm than the A/B would have given it.
        return _as_utc(born_at) >= _as_utc(cutoff)
    except Exception:
        LOG.warning(
            "Failed to read the workflow's birth timestamp for the Task V3 default rule",
            workflow_permanent_id=workflow_permanent_id,
            exc_info=True,
        )
        return False


async def _ab_flag_puts_run_in_treatment(
    provider: BaseExperimentationProvider,
    *,
    workflow_run_id: str,
    organization_id: str | None,
    workflow_permanent_id: str | None,
    billing_tier: BillingTier,
) -> bool | None:
    """Whether the percentage knob buckets this run into treatment, or ``None`` if it never answered.

    ``resolve_feature_flag_strict`` rather than the boolean resolver so neither answer that is not a
    bucket collapses into one: both arms leave the same engine on a control run, so a swallowed error
    or an unresolved key would otherwise enter every per-arm read as a bucketed control run that was
    never randomized. A raised evaluation propagates to the resolver's catch-all and is labelled
    ``flag_error``; ``None`` -- the key is absent from the local snapshot, or carries a condition
    local evaluation cannot answer -- is returned as itself and labelled ``flag_undefined``. The arm
    is control for all three. Cached on the same (flag, distinct_id, properties) key the boolean
    resolver was, in the provider's own strict map, so this stays one evaluation per run.
    """
    return await provider.resolve_feature_flag_strict(
        WORKFLOW_TASK_V3_AB_FLAG,
        workflow_run_id,
        properties={
            "organization_id": organization_id,
            "workflow_permanent_id": workflow_permanent_id or "not_workflow",
            BILLING_TIER_PROPERTY: billing_tier.value,
        },
    )


async def _resolve_new_workflow_default_rollout(
    provider: BaseExperimentationProvider,
    *,
    workflow_run_id: str,
    organization_id: str | None,
    workflow_permanent_id: str | None,
    billing_tier: BillingTier,
) -> NewWorkflowDefaultRollout:
    """How the rollout flag answered for a run the new-workflow v3 default would otherwise enrol.

    This flag is what enrols: the share it does not enrol continues down ``WORKFLOW_TASK_V3_AB``
    exactly as if the rule did not exist, which is where this population's concurrent control comes
    from. It is also the only lever that stops the rule for less than everybody -- the cutoff is a
    setting and needs a restart, and a condition on ``WORKFLOW_TASK_V3_AB`` cannot reach a run that
    never evaluates it. Turning it off de-enrols rather than forcing v1: an unenrolled run can still
    be treated by the A/B, so ``DISABLE_TASK_V3`` is the lever for getting an organization off v3.

    ``resolve_feature_flag_strict`` rather than the boolean resolver, so an evaluation that raised is
    labelled rather than collapsed: both PostHog providers swallow one into ``None`` and ``bool()`` it
    to ``False``, which would report an error as a real "outside the percentage".

    Only a conclusive ``True`` enrols. ``False``, ``None`` and a raised evaluation all leave the run on
    the A/B, so every off-gesture on this flag -- disable, delete, 0%, an excluding condition -- turns
    the rule off, and the cutoff does nothing until the flag resolves ``True`` for someone. A
    condition on it must be written on person properties: the arm-resolving processes evaluate
    PostHog locally, and a cohort, group or flag-dependency condition resolves to ``None``, which
    enrols nobody.
    """
    try:
        resolved = await provider.resolve_feature_flag_strict(
            TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT_FLAG,
            workflow_run_id,
            properties={
                "organization_id": organization_id,
                "workflow_permanent_id": workflow_permanent_id,
                BILLING_TIER_PROPERTY: billing_tier.value,
            },
        )
    except Exception:
        LOG.warning(
            "Failed to evaluate the new-workflow v3 default rollout; leaving this run on the A/B",
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )
        return NewWorkflowDefaultRollout.error
    if resolved is None:
        return NewWorkflowDefaultRollout.undefined
    return NewWorkflowDefaultRollout.enrolled if resolved else NewWorkflowDefaultRollout.not_enrolled


async def resolve_workflow_block_engine_arm(
    context: skyvern_context.SkyvernContext,
    *,
    workflow_run_id: str,
    organization_id: str | None,
    workflow_permanent_id: str | None,
    workflow_status: WorkflowStatus,
    trigger_type: WorkflowRunTriggerType | None,
    ineligibility_reason: V3AbIneligibleReason | None,
) -> None:
    """Resolve the workflow-block engine A/B once at execution start and pin the arm on the context.

    Bucketed per workflow run so every block of one run shares an arm. Idempotent per run: once
    resolved, a TTL expiry or a mid-run flag ramp cannot flip it. The pin records the run it was
    resolved for, so a nested execution sharing this context -- an inline child workflow run has its
    own workflow_run_id and its own definition -- re-resolves instead of inheriting an arm that was
    never checked against its blocks. Control (no override) is the safe default and the outcome of
    every failure.

    A self-serve organization's workflow born at or after
    ``settings.TASK_V3_DEFAULT_ENGINE_WORKFLOW_CUTOFF`` takes v3 without the percentage being
    consulted, so those runs are marked ``new_self_serve_workflow_default`` for every per-arm read to
    exclude. ``TASK_V3_NEW_WORKFLOW_DEFAULT_ROLLOUT``, read only for the runs that rule would enrol,
    is what enrols them; the share it leaves alone continues down the A/B, which is where this
    population's concurrent control and its scoped kill come from. The rule fires only on a
    conclusive True, so the cutoff is inert until the flag resolves True for someone, and disabling,
    deleting or zeroing the flag turns the rule off.

    ``workflow_status`` is the status of the version this run executes and ``trigger_type`` is how
    the run was launched. Together they are what separates a workflow a customer keeps from the
    per-call throwaways several endpoints mint and run in one request: most of those run while still
    auto_generated, and the job-recipe endpoints build a published definition, so neither test alone
    covers them.
    """
    if context.workflow_block_engine_resolved_run_id == workflow_run_id:
        return
    provider = app.EXPERIMENTATION_PROVIDER
    if isinstance(provider, NoOpExperimentationProvider):
        # No experimentation configured (OSS default) -> control, and never query the provider.
        context.workflow_block_engine_override = None
        context.workflow_block_engine_arm_decision = NO_ARM_DECISION
        context.workflow_block_engine_resolved_run_id = workflow_run_id
        return
    async with context.workflow_block_engine_lock:
        if context.workflow_block_engine_resolved_run_id == workflow_run_id:
            return
        override: RunEngine | None = None
        run_is_eligible = ineligibility_reason is None
        billing_tier: BillingTier | None = None
        route_reason = WorkflowBlockEngineRouteReason.ineligible
        rollout: NewWorkflowDefaultRollout | None = None
        try:
            if run_is_eligible:
                # The kill switch, shared with the dispatch gate via task_v3_disabled so both
                # evaluations use the same cache key, wins over the experiment and over the
                # new-workflow default below. A kill flipped mid-run still takes effect at dispatch
                # while these rows already read v3: stopping the run wins over its attribution. Read
                # FIRST, and before the billing lookup below: a Redis or pooler incident is exactly
                # when someone flips this, and this lock is held throughout, so the kill switch must
                # not queue behind that call.
                if await task_v3_disabled(workflow_run_id, organization_id):
                    route_reason = WorkflowBlockEngineRouteReason.disabled
                else:
                    # billing_tier rides along so a release condition can hold enterprise and
                    # self-serve at different percentages. Deliberately not passed to
                    # task_v3_disabled: that flag's two callers must build identical provider cache
                    # keys, see its docstring.
                    billing_tier = await _billing_tier_for_arm(organization_id)
                    # The tier, the status and the trigger are checked first, so anything that
                    # cannot use the rule pays for no workflow read. An auto_generated running
                    # version is a per-call workflow the login, download_files, credential
                    # test-login and SDK endpoints mint and run in one request, and enrolling those
                    # would move standing API traffic rather than the workflows a customer keeps;
                    # the birth version's status would be the wrong test, because the prompt box
                    # writes version 1 as auto_generated and the editor save that keeps it writes
                    # the next as published. The job-recipe endpoints are per-call too but build a
                    # published definition, so only their trigger kind excludes them.
                    takes_new_workflow_default = (
                        billing_tier == BillingTier.SELF_SERVE
                        and workflow_status != WorkflowStatus.auto_generated
                        and trigger_type not in PER_CALL_WORKFLOW_RUN_TRIGGER_TYPES
                        and await _workflow_is_new_for_v3_default(workflow_permanent_id, organization_id)
                    )
                    if takes_new_workflow_default:
                        # Read only for the runs the rule would enrol, so the A/B population is never
                        # exposed to this flag and its bucketing is unchanged.
                        rollout = await _resolve_new_workflow_default_rollout(
                            provider,
                            workflow_run_id=workflow_run_id,
                            organization_id=organization_id,
                            workflow_permanent_id=workflow_permanent_id,
                            billing_tier=billing_tier,
                        )
                    if takes_new_workflow_default and _rollout_enrols(rollout):
                        # A workflow born at or after the cutoff runs its task blocks on v3 by
                        # default, so the percentage knob is never consulted: the unenrolled share the
                        # A/B then routes to control is the only concurrent control these runs have.
                        override = RunEngine.skyvern_v3
                        route_reason = WorkflowBlockEngineRouteReason.new_self_serve_workflow_default
                    else:
                        in_treatment = await _ab_flag_puts_run_in_treatment(
                            provider,
                            workflow_run_id=workflow_run_id,
                            organization_id=organization_id,
                            workflow_permanent_id=workflow_permanent_id,
                            billing_tier=billing_tier,
                        )
                        if in_treatment:
                            override = RunEngine.skyvern_v3
                            route_reason = WorkflowBlockEngineRouteReason.flag_bucket_treatment
                        elif in_treatment is None:
                            # The flag never answered, so this run is on control without having been
                            # randomized. Labelled apart from the bucket so the control cell every
                            # per-arm read builds out of flag_bucket_control stays a randomized one.
                            route_reason = WorkflowBlockEngineRouteReason.flag_undefined
                        else:
                            route_reason = WorkflowBlockEngineRouteReason.flag_bucket_control
        except Exception:
            LOG.warning(
                "Failed to resolve the workflow-block engine arm; using control",
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
            override = None
            route_reason = WorkflowBlockEngineRouteReason.flag_error
        decision = WorkflowBlockEngineArmDecision(
            route_reason=route_reason,
            billing_tier=billing_tier,
            new_workflow_default_rollout_resolution=rollout,
        )
        context.workflow_block_engine_override = override
        context.workflow_block_engine_arm_decision = decision
        # Last, so every reader gated on this sentinel sees the override and the decision it was
        # resolved with rather than a previous run's.
        context.workflow_block_engine_resolved_run_id = workflow_run_id
        LOG.info(
            "Resolved workflow-block engine arm",
            workflow_run_id=workflow_run_id,
            workflow_permanent_id=workflow_permanent_id,
            arm=_arm_label(override),
            route_reason=engine_arm_log_value(route_reason),
            # True only for a run this rule enrolled, which is every run it treated: the rule's
            # control cell is the unenrolled share the A/B then routed to control, so a read of the
            # rule compares route_reason=new_self_serve_workflow_default against
            # new_workflow_default_rollout_resolution=not_enrolled intersected with
            # flag_bucket_control -- an unenrolled run re-enters the A/B and can be treated there.
            new_workflow_default_rollout=_rollout_enrols(rollout),
            # None whenever the rollout flag was not consulted. The three non-enrolling resolutions
            # are indistinguishable in the boolean above, and only not_enrolled is a randomized cell,
            # so both a check of whether the flag is answering at all and the rule's control cell read
            # this field instead.
            new_workflow_default_rollout_resolution=engine_arm_log_value(rollout),
            run_is_eligible=run_is_eligible,
            ineligibility_reason=ineligibility_reason,
            # None whenever the A/B was never consulted -- an ineligible run, or one the kill switch
            # already stopped -- so the field means "the tier this run was bucketed on" and never
            # doubles as "nobody looked".
            billing_tier=engine_arm_log_value(billing_tier),
        )


def workflow_block_engine_override(workflow_run_id: str | None) -> RunEngine | None:
    """The engine the A/B pinned for this run, or None when the run is control or unresolved.

    Returns None unless the pin was resolved for this exact run, so an execution path that never
    reaches the resolver (task_v2, the cached-script block helpers) keeps its declared engine.
    """
    if not workflow_run_id:
        return None
    context = skyvern_context.current()
    if context is None or context.workflow_block_engine_resolved_run_id != workflow_run_id:
        return None
    return context.workflow_block_engine_override


def resolved_workflow_block_engine_arm_attribution(workflow_run_id: str | None) -> WorkflowBlockEngineArmAttribution:
    """The arm and the routing facts finalize-time telemetry can attribute to this run.

    Which of the three arm cases a context is in, and why they are kept apart, is the
    ``WorkflowBlockEngineArmAttribution`` contract; this reads it off the run's own context. The
    decision facts come from the pin the resolver wrote when it decided the arm, never from a second
    lookup, so a run's duration log cannot report a tier its routing line did not bucket on -- and
    finalization pays for no billing read at all (SKY-16122).
    """
    if not workflow_run_id:
        return ARM_ATTRIBUTION_LOST
    context = skyvern_context.current()
    if context is None or context.workflow_run_id != workflow_run_id:
        return ARM_ATTRIBUTION_LOST
    if context.workflow_block_engine_resolved_run_id != workflow_run_id:
        return WorkflowBlockEngineArmAttribution(arm=None)
    # Derived from the engine the run routes on rather than read out of the pin, so the arm and the
    # engine cannot drift apart.
    arm = _arm_label(context.workflow_block_engine_override)
    decision = context.workflow_block_engine_arm_decision
    return WorkflowBlockEngineArmAttribution(arm=arm, decision=decision or NO_ARM_DECISION)
