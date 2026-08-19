from collections.abc import Mapping
from hashlib import sha256
from typing import Any
from urllib.parse import quote

from jinja2.sandbox import SandboxedEnvironment

from skyvern.forge.sdk.workflow.browser_profile_key import render_browser_profile_key
from skyvern.forge.sdk.workflow.context_manager import resolve_credential_parameter_binding
from skyvern.forge.sdk.workflow.models.block import LoginBlock, get_all_blocks
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter, WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow

_sequential_key_jinja_env = SandboxedEnvironment()

_MAX_RAW_REUSE_IDENTITY_BYTES = 256


REUSE_ADMISSION_OFF_PREFIX = "off:"
REUSE_ADMISSION_OFF_KILL_SWITCH = "off:kill_switch"
REUSE_ADMISSION_OFF_UNRESOLVABLE = "off:unresolvable"
REUSE_ADMISSION_OFF_DISABLED = "off:disabled"


def is_reuse_admission_off(reuse_bound_key: str | None) -> bool:
    """Return whether the resolved admission tuple explicitly disables browser reuse."""
    return reuse_bound_key is not None and reuse_bound_key.startswith(REUSE_ADMISSION_OFF_PREFIX)


def _build_reuse_identity(namespace: str, raw_identity: str) -> str:
    """Return an index-safe, namespaced identity without weakening equality semantics."""
    encoded_identity = raw_identity.encode("utf-8")
    if len(encoded_identity) <= _MAX_RAW_REUSE_IDENTITY_BYTES:
        return f"{namespace}:{raw_identity}"
    return f"{namespace}:sha256:{sha256(encoded_identity).hexdigest()}"


def render_sequential_key(
    sequential_key: str,
    parameters: dict[str, Any],
    persisted_selections: dict[str, str],
) -> tuple[str, dict[str, Any], list[str]]:
    """Render a configured sequential-key template against one shared parameter context.

    Persisted credential selections form the base context. Run parameters take precedence when
    keys collide. The returned collision list lets callers report ambiguous inputs consistently.
    An empty context leaves the configured key unchanged.
    """
    colliding = sorted(set(parameters) & set(persisted_selections)) if persisted_selections else []
    merged: dict[str, Any] = {**persisted_selections, **parameters} if persisted_selections else dict(parameters)
    rendered = _sequential_key_jinja_env.from_string(sequential_key).render(merged) if merged else sequential_key
    return rendered, merged, colliding


def resolve_reuse_bound_key(
    workflow: Workflow,
    parameters: Mapping[str, Any],
    persisted_selections: Mapping[str, str],
) -> tuple[str, list[str]]:
    """Resolve one namespaced browser-reuse identity from persisted run inputs.

    Credential selections have highest precedence. A run with several login credentials gets one
    deterministic, role-keyed composite identity. Profile and configured sequential templates are fallbacks;
    the run's max-concurrency slot is intentionally not an input. An otherwise-unkeyed workflow
    resolves to its permanent-ID sentinel, so ``None`` always means that reuse is not admitted.
    The ``wf:`` sentinel is the explicit single-account contract: every run of the workflow shares
    one browser and inherits its authenticated state. A workflow that serves multiple accounts must
    partition reuse with login credentials, a browser profile key, or a sequential key template.
    Every raw namespace identity is capped before it reaches an indexed database column.
    """
    credential_bindings: set[tuple[str, str]] = set()
    for block in get_all_blocks(workflow.workflow_definition.blocks):
        if not isinstance(block, LoginBlock):
            continue
        for parameter in block.parameters:
            if isinstance(parameter, CredentialParameter):
                selected_credential_id = persisted_selections.get(parameter.key)
                if parameter.credential_ids and selected_credential_id is None:
                    raise ValueError(f"Rotating login credential {parameter.key!r} has no persisted selection")
                credential_bindings.add(
                    (
                        parameter.key,
                        resolve_credential_parameter_binding(
                            parameter,
                            parameters,
                            selected_credential_id,
                        ),
                    )
                )
            elif (
                isinstance(parameter, WorkflowParameter)
                and parameter.workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID
            ):
                credential_id = parameters.get(parameter.key)
                if credential_id is None:
                    continue
                if not isinstance(credential_id, str) or not credential_id:
                    raise ValueError(f"Login credential parameter {parameter.key!r} is not a credential id")
                credential_bindings.add((parameter.key, credential_id))

    encoded_credential_bindings = [
        (quote(parameter_key, safe=""), quote(credential_id, safe=""))
        for parameter_key, credential_id in sorted(credential_bindings)
    ]
    if len(encoded_credential_bindings) == 1:
        return _build_reuse_identity("cred", encoded_credential_bindings[0][1]), []
    if encoded_credential_bindings:
        composite_identity = "+".join(
            f"{parameter_key}={credential_id}" for parameter_key, credential_id in encoded_credential_bindings
        )
        return _build_reuse_identity("creds", composite_identity), []

    colliding = sorted(set(parameters) & set(persisted_selections))
    merged: dict[str, Any] = {**persisted_selections, **parameters}
    if workflow.browser_profile_key:
        rendered_profile_key = render_browser_profile_key(workflow.browser_profile_key, merged)
        if not rendered_profile_key:
            raise ValueError("Browser profile key rendered to an empty reuse identity")
        return _build_reuse_identity("profile", rendered_profile_key), colliding

    if workflow.sequential_key:
        rendered_sequential_key, _, colliding = render_sequential_key(
            workflow.sequential_key,
            dict(parameters),
            dict(persisted_selections),
        )
        if not rendered_sequential_key:
            raise ValueError("Sequential key rendered to an empty reuse identity")
        return _build_reuse_identity("seq", rendered_sequential_key), colliding

    return _build_reuse_identity("wf", workflow.workflow_permanent_id), colliding
