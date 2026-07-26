"""Enrollment of workflow versions in the browser action firewall (SKY-12873).

Persistence, eligibility and run binding for :mod:`skyvern.forge.sdk.browser_action_policy`. The
policy is stored as a reserved key inside the existing ``workflow_definition`` JSON rather than as a
field on :class:`WorkflowDefinition`: pydantic drops unknown keys, so no request model can carry a
policy in and no response can echo one out. That same drop would silently erase an enrollment on any
read-modify-write through the model, so every write goes through :func:`with_policy`.

An enrolled policy is a CEILING, not a complete authority: it records that an operator protected this
workflow version and the widest origin set they authorized. It does not say what a given action may
reach at this moment, and a consumer must not read "inside the enrolled set" as sufficient to allow.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from skyvern.exceptions import BrowserActionPolicyNotEnforceable
from skyvern.forge.sdk.browser_action_policy import BrowserActionPolicy, declare_policy
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.workflow.models.block import BlockType, get_all_blocks
from skyvern.forge.sdk.workflow.models.workflow import Workflow

POLICY_KEY = "browser_action_policy"

_STORED_FIELDS = frozenset({"owner_id", "allowed_origins", "version"})


class EnrollmentRejection(StrEnum):
    """Why a workflow version cannot run under a policy. Values are stable and carry no content."""

    CODE_BLOCK_PRESENT = "code_block_present"
    CODE_EXECUTION_SELECTED = "code_execution_selected"
    ADAPTIVE_CACHING_ENABLED = "adaptive_caching_enabled"
    SCRIPT_GENERATION_ENABLED = "script_generation_enabled"


def serialize_policy(policy: BrowserActionPolicy) -> dict[str, Any]:
    """`allowed_origins` is a frozenset and frozensets have no order — sorting the canonical forms is
    what makes two equal policies serialize to identical JSON."""
    return {
        "owner_id": policy.owner_id,
        "allowed_origins": sorted(origin.canonical for origin in policy.allowed_origins),
        "version": policy.version,
    }


def deserialize_policy(raw: object) -> BrowserActionPolicy:
    """Rebuild a stored policy, raising ValueError on anything the declaration rules reject."""
    if not isinstance(raw, dict):
        raise ValueError(f"Stored browser action policy is not an object: {type(raw).__name__}")
    if set(raw) != _STORED_FIELDS:
        raise ValueError(f"Stored browser action policy has unexpected fields: {sorted(raw)}")
    owner_id = raw["owner_id"]
    origins = raw["allowed_origins"]
    version = raw["version"]
    if not isinstance(owner_id, str):
        raise ValueError("Stored browser action policy has a non-string owner")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("Stored browser action policy has a non-integer version")
    if not isinstance(origins, list) or not all(isinstance(origin, str) for origin in origins):
        raise ValueError("Stored browser action policy origins are not a list of strings")
    return declare_policy(owner_id=owner_id, origin_urls=origins, version=version)


def read_policy(workflow_definition: object) -> BrowserActionPolicy | None:
    """The version's policy, or None when it is unenrolled. Raises when it is enrolled but unusable —
    an enrolled version whose policy cannot be read must never degrade to an unenrolled run."""
    if not isinstance(workflow_definition, dict) or POLICY_KEY not in workflow_definition:
        return None
    return deserialize_policy(workflow_definition[POLICY_KEY])


def carried_policy(workflow_definition: object) -> object | None:
    """The stored policy value, verbatim. Never validates: a save carries policy forward untouched,
    and a save that could repair a corrupt policy could also alter a sound one."""
    if not isinstance(workflow_definition, dict):
        return None
    return workflow_definition.get(POLICY_KEY)


def with_policy(workflow_definition: dict[str, Any], stored_policy: object) -> dict[str, Any]:
    """A copy of the definition carrying exactly `stored_policy`, dropping any key the caller sent."""
    definition = {key: value for key, value in workflow_definition.items() if key != POLICY_KEY}
    if stored_policy is not None:
        definition[POLICY_KEY] = stored_policy
    return definition


def stored_policy_version(workflow_definition: object) -> int:
    """Best-effort read of the persisted policy version; 0 when absent or unreadable.

    Never raises: replacing an unreadable policy is the repair path, and it must still advance the
    counter rather than reissue a version some run may already have bound.
    """
    raw = carried_policy(workflow_definition)
    if not isinstance(raw, dict):
        return 0
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        return 0
    return version


def rejection_reasons(workflow: Workflow, *, run_with: str | None) -> tuple[EnrollmentRejection, ...]:
    """Configurations an action-level firewall cannot cover, because the browser work happens inside
    generated or operator-supplied code that never reaches an action sink."""
    reasons: list[EnrollmentRejection] = []
    if any(block.block_type == BlockType.CODE for block in get_all_blocks(workflow.workflow_definition.blocks)):
        reasons.append(EnrollmentRejection.CODE_BLOCK_PRESENT)
    # Either level selecting code is disqualifying: a single agent-mode run does not make a
    # code-mode version enforceable, and a run-level override can pick code on an agent version.
    if workflow.run_with == "code" or run_with == "code":
        reasons.append(EnrollmentRejection.CODE_EXECUTION_SELECTED)
    if workflow.adaptive_caching:
        reasons.append(EnrollmentRejection.ADAPTIVE_CACHING_ENABLED)
    if workflow.generate_script_on_terminal:
        reasons.append(EnrollmentRejection.SCRIPT_GENERATION_ENABLED)
    return tuple(reasons)


def bind_policy_to_context(
    policy: BrowserActionPolicy | None,
    workflow: Workflow,
    *,
    run_with: str | None,
) -> None:
    """Bind the resolved workflow version's policy to the current run.

    An unenrolled version clears the slot rather than leaving it alone, so a grant cannot survive
    into a run, child workflow or reused context that did not earn it.
    """
    if policy is None:
        context = skyvern_context.current()
        if context is not None:
            context.browser_action_policy = None
        return

    reasons = rejection_reasons(workflow, run_with=run_with)
    if reasons:
        raise BrowserActionPolicyNotEnforceable(reasons)
    # ensure_context rather than a silent skip: with no context there is nothing to consume the
    # policy, and an enrolled run must not proceed as though it had been bound.
    skyvern_context.ensure_context().browser_action_policy = policy
