"""Copilot agent tools — native handlers, hooks, and registration."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse

import structlog
import yaml

try:
    from bs4 import BeautifulSoup  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — bs4 is a transitive dep but discovery degrades gracefully without it.
    BeautifulSoup = None  # type: ignore[assignment, misc]
from jinja2.sandbox import SandboxedEnvironment
from pydantic import ValidationError

from skyvern.forge import app
from skyvern.forge.sdk.copilot.block_goal_wrapping import wrap_workflow_block_goals
from skyvern.forge.sdk.copilot.build_phase import (
    BuildPhase,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.failure_tracking import (
    _canonical_block_config,
)
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
)
from skyvern.forge.sdk.routes.workflow_copilot import _process_workflow_yaml
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException
from skyvern.forge.sdk.workflow.models.block import BlockTypeVar, get_all_blocks
from skyvern.forge.sdk.workflow.models.parameter import (
    RESERVED_PARAMETER_KEYS,
    Parameter,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.schemas.workflows import BlockType

from ._shared import (
    _block_type_name,
    _valid_runtime_anchor_url,
    _workflow_definition_block_labels,
    _workflow_yaml_blocks_by_label,
)

LOG = structlog.get_logger()


_BLOCK_TYPES_STATE_ESTABLISHER = frozenset({"navigation", "login", "goto_url"})

_JINJA_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_OUTPUT_REF_RE = re.compile(rf"\{{\{{\s*({_JINJA_IDENTIFIER})_output\s*(?=[\.|}}])")
_BLOCK_FORM_REF_RE = re.compile(rf"\{{\{{\s*({_JINJA_IDENTIFIER})\s*\.")
_JINJA_ROOT_RE = re.compile(rf"\{{\{{\s*({_JINJA_IDENTIFIER})\s*(?=[\.|}}])")

_JINJA_RUNTIME_GLOBAL_ROOTS = frozenset(SandboxedEnvironment().globals)
_JINJA_LITERAL_ROOTS = frozenset({"none", "true", "false"})
_JINJA_SPECIAL_CONTEXT_ROOTS = frozenset({"loop", "self", "varargs", "kwargs"})
_SKYVERN_TEMPLATE_CONTEXT_ROOTS = frozenset(RESERVED_PARAMETER_KEYS) | frozenset(
    {
        "parameters",
        "browser_session_id",
        "organization_id",
        # Conditional / branch evaluation roots — see BranchEvaluationContext.build_template_data.
        "params",
        "outputs",
        "environment",
        "env",
        "llm",
    }
)
_TEMPLATE_BUILTIN_ROOTS = (
    _JINJA_RUNTIME_GLOBAL_ROOTS | _JINJA_LITERAL_ROOTS | _JINJA_SPECIAL_CONTEXT_ROOTS | _SKYVERN_TEMPLATE_CONTEXT_ROOTS
)

# Keep this to grammatical glue only. Workflow/action words are intentionally
# not filtered; the two-token stale threshold is the conservative guardrail.
_BLOCK_METADATA_STOPWORDS = frozenset({"and", "for", "the", "with"})


def _blocks_by_label(workflow_definition: object | None) -> dict[str, object]:
    blocks = getattr(workflow_definition, "blocks", None) if workflow_definition else None
    by_label: dict[str, object] = {}
    if not blocks:
        return by_label
    for block in blocks:
        label = getattr(block, "label", None)
        if isinstance(label, str):
            by_label[label] = block
    return by_label


# Minimum length to apply the trailing-``s`` plural strip; below this we
# leave the token alone so words like ``is``/``us``/``has`` aren't mangled.
_MIN_STEMMABLE_TOKEN_LEN = 5


def _metadata_token(token: str) -> str:
    token = token.lower()
    if len(token) >= _MIN_STEMMABLE_TOKEN_LEN and token.endswith("s"):
        token = token[:-1]
    return token


def _metadata_tokens(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    tokens: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9]+", value):
        normalized = _metadata_token(token)
        if len(normalized) <= 2 or normalized in _BLOCK_METADATA_STOPWORDS:
            continue
        tokens.add(normalized)
    return tokens


def _semantic_tokens_from_yaml(value: Any, *, exclude_keys: frozenset[str]) -> set[str]:
    tokens: set[str] = set()
    if isinstance(value, str):
        return _metadata_tokens(value)
    if isinstance(value, list):
        for item in value:
            tokens.update(_semantic_tokens_from_yaml(item, exclude_keys=exclude_keys))
        return tokens
    if isinstance(value, dict):
        for key, item in value.items():
            if key in exclude_keys:
                continue
            tokens.update(_semantic_tokens_from_yaml(item, exclude_keys=exclude_keys))
    return tokens


def _stale_metadata_reason(
    *,
    field_name: str,
    field_value: Any,
    prior_block: dict[str, Any],
    submitted_block: dict[str, Any],
    current_exclude_keys: frozenset[str],
) -> str | None:
    tokens = _metadata_tokens(field_value)
    if len(tokens) < 2:
        return None

    prior_tokens = _semantic_tokens_from_yaml(prior_block, exclude_keys=current_exclude_keys)
    current_tokens = _semantic_tokens_from_yaml(submitted_block, exclude_keys=current_exclude_keys)
    removed_tokens = prior_tokens - current_tokens
    if len(tokens & removed_tokens) < 2:
        return None

    return f"{field_name} {field_value!r} appears stale"


def _prior_blocks_by_unique_title(prior_by_label: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    titled = [(b["title"], b) for b in prior_by_label.values() if isinstance(b.get("title"), str) and b["title"]]
    counts = Counter(title for title, _ in titled)
    return {title: block for title, block in titled if counts[title] == 1}


_STALE_BASE_EXCLUDE = frozenset({"label", "next_block_label"})
_STALE_TITLE_EXCLUDE = frozenset({"label", "title", "next_block_label"})


def _stale_for_renamed_label(
    label: str,
    submitted_block: dict[str, Any],
    prior_by_unique_title: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    submitted_title = submitted_block.get("title")
    # Title-less blocks intentionally skip the renamed-label path: with no
    # title there is no cross-reference to a prior block, so we'd be
    # guessing whether the rename was warranted.
    if not (isinstance(submitted_title, str) and submitted_title):
        return None
    prior_block = prior_by_unique_title.get(submitted_title)
    if prior_block is None:
        return None
    title_reason = _stale_metadata_reason(
        field_name="title",
        field_value=submitted_title,
        prior_block=prior_block,
        submitted_block=submitted_block,
        current_exclude_keys=_STALE_TITLE_EXCLUDE,
    )
    if not title_reason:
        return None
    return {"label": label, "reasons": [title_reason]}


def _stale_for_matched_label(
    label: str,
    submitted_block: dict[str, Any],
    prior_block: dict[str, Any],
) -> dict[str, Any] | None:
    reasons: list[str] = []
    submitted_title = submitted_block.get("title")
    prior_title = prior_block.get("title")
    title_reason = None
    if isinstance(submitted_title, str) and submitted_title and submitted_title == prior_title:
        title_reason = _stale_metadata_reason(
            field_name="title",
            field_value=submitted_title,
            prior_block=prior_block,
            submitted_block=submitted_block,
            current_exclude_keys=_STALE_TITLE_EXCLUDE,
        )
        if title_reason:
            reasons.append(title_reason)

    # When the title was already flagged stale, exclude its tokens from the
    # label-stale comparison so the same words don't double-count; otherwise
    # the title's tokens are stable content that should weigh in.
    label_exclude_keys = _STALE_TITLE_EXCLUDE if title_reason else _STALE_BASE_EXCLUDE
    label_reason = _stale_metadata_reason(
        field_name="label",
        field_value=label,
        prior_block=prior_block,
        submitted_block=submitted_block,
        current_exclude_keys=label_exclude_keys,
    )
    if label_reason:
        reasons.insert(0, label_reason)

    if not reasons:
        return None
    return {"label": label, "reasons": reasons}


def _detect_stale_block_metadata(submitted_yaml: str | None, prior_yaml: str | None) -> list[dict[str, Any]]:
    """Find corrected blocks whose old label/title no longer matches their revised goal text."""
    prior_by_label = _workflow_yaml_blocks_by_label(prior_yaml)
    submitted_by_label = _workflow_yaml_blocks_by_label(submitted_yaml)
    if not prior_by_label or not submitted_by_label:
        return []

    prior_by_unique_title = _prior_blocks_by_unique_title(prior_by_label)

    stale_items: list[dict[str, Any]] = []
    for label, submitted_block in submitted_by_label.items():
        prior_block = prior_by_label.get(label)
        if prior_block is None:
            item = _stale_for_renamed_label(label, submitted_block, prior_by_unique_title)
        else:
            item = _stale_for_matched_label(label, submitted_block, prior_block)
        if item is not None:
            stale_items.append(item)
    return stale_items


_STALE_BLOCK_METADATA_MESSAGE_LIMIT = 5


def _stale_block_metadata_message(items: list[dict[str, Any]]) -> str:
    details = []
    for item in items[:_STALE_BLOCK_METADATA_MESSAGE_LIMIT]:
        label = item.get("label", "?")
        reasons = item.get("reasons") or []
        detail = "; ".join(str(reason) for reason in reasons)
        details.append(f"{label}: {detail}")
    if len(items) > _STALE_BLOCK_METADATA_MESSAGE_LIMIT:
        details.append(f"(and {len(items) - _STALE_BLOCK_METADATA_MESSAGE_LIMIT} more)")
    joined = "; ".join(details)
    return (
        "Workflow validation failed: corrected block metadata still appears stale. "
        "When changing a user's requested subject, URL, or action, rename affected block labels and titles "
        "to match the revised goal, and update next_block_label, block_labels, and Jinja references accordingly. "
        f"Stale metadata: {joined}"
    )


def _find_invalidated_labels(
    old_definition: object | None,
    new_definition: object | None,
    requested_labels: list[str],
) -> set[str]:
    """Return the set of requested labels whose behavior is invalidated.

    A label is invalidated when its own config changed or when any upstream
    label in the requested chain was invalidated (downstream trust propagates
    forward).
    """
    old_by_label = _blocks_by_label(old_definition)
    new_by_label = _blocks_by_label(new_definition)
    invalidated: set[str] = set()
    upstream_invalidated = False
    for label in requested_labels:
        if upstream_invalidated:
            invalidated.add(label)
            continue
        old_block = old_by_label.get(label)
        new_block = new_by_label.get(label)
        if old_block is None or new_block is None:
            invalidated.add(label)
            upstream_invalidated = True
            continue
        if _canonical_block_config(old_block) != _canonical_block_config(new_block):
            invalidated.add(label)
            upstream_invalidated = True
    return invalidated


def _earliest_invalidated(requested_labels: list[str], invalidated: set[str]) -> str | None:
    for label in requested_labels:
        if label in invalidated:
            return label
    return None


def _clear_runtime_anchor_evidence(copilot_ctx: Any) -> None:
    # Clears the runtime-anchor *trust* flags only. evidence.current_url /
    # page_title / workflow_run_id are left intact: an edit does not move the
    # browser, so they remain accurate observational context — the cleared flags
    # are what mark that state as no longer verified.
    evidence = copilot_ctx.workflow_verification_evidence
    copilot_ctx.verified_prefix_current_url = None
    evidence.live_page_state_verified = False
    evidence.verified_from_current_browser_state = False
    evidence.current_url_observed_after_workflow_run = False
    evidence.current_url_may_encode_runtime_state = False


def _reset_all_verified_trust(copilot_ctx: Any) -> None:
    evidence = copilot_ctx.workflow_verification_evidence
    copilot_ctx.verified_prefix_labels = []
    copilot_ctx.verified_block_outputs = {}
    copilot_ctx.last_full_workflow_test_ok = False
    evidence.block_verified = []
    evidence.full_workflow_verified = False
    _clear_runtime_anchor_evidence(copilot_ctx)


def _workflow_parameters_changed(prior_definition: object | None, new_definition: object | None) -> bool:
    new_by_key = {getattr(p, "key", None): p for p in (getattr(new_definition, "parameters", None) or [])}
    prior_by_key = {getattr(p, "key", None): p for p in (getattr(prior_definition, "parameters", None) or [])}
    # Any added or removed parameter is a change: a block may reference a key by
    # template without a config edit, so an added/removed key can alter behavior
    # the block-diff alone won't catch. Pure reordering is ignored (keyed access).
    if set(prior_by_key) != set(new_by_key):
        return True
    for key, prior_param in prior_by_key.items():
        try:
            if _stable_parameter_fingerprint(prior_param) != _stable_parameter_fingerprint(new_by_key[key]):
                return True
        except Exception:
            LOG.debug("Parameter fingerprint comparison failed on edit", exc_info=True)
            return True
    return False


def _invalidate_verified_state_on_edit(
    copilot_ctx: Any,
    prior_definition: object | None,
    new_definition: object | None,
) -> None:
    evidence = copilot_ctx.workflow_verification_evidence
    if new_definition is None:
        # An unknown new definition can't be reconciled against; fail closed.
        if copilot_ctx.verified_prefix_labels or evidence.block_verified:
            _reset_all_verified_trust(copilot_ctx)
        return
    prior_labels = _workflow_definition_block_labels(prior_definition)
    trusted = set(copilot_ctx.verified_prefix_labels or []) | set(evidence.block_verified or [])

    invalidated: set[str] = set()
    if trusted:
        # No reconcilable prior, or a parameter change that could alter any
        # block's behavior — fail closed rather than reuse unproven trust.
        if prior_definition is None or _workflow_parameters_changed(prior_definition, new_definition):
            _reset_all_verified_trust(copilot_ctx)
            return
        # Diff the full prior chain, not just trusted labels, so a change to an
        # unverified upstream block still propagates to downstream trusted ones.
        full_order = list(prior_labels)
        for label in list(copilot_ctx.verified_prefix_labels or []) + list(evidence.block_verified or []):
            if label not in full_order:
                full_order.append(label)
        try:
            invalidated = _find_invalidated_labels(prior_definition, new_definition, full_order) & trusted
        except Exception:
            LOG.debug("Verified-state invalidation diff failed on edit", exc_info=True)
            _reset_all_verified_trust(copilot_ctx)
            return

    if invalidated:
        copilot_ctx.verified_prefix_labels = [
            label for label in copilot_ctx.verified_prefix_labels if label not in invalidated
        ]
        for label in invalidated:
            copilot_ctx.verified_block_outputs.pop(label, None)
        evidence.block_verified = [label for label in evidence.block_verified if label not in invalidated]
        # The recorded prefix-end URL came from a run that included a now-invalid
        # block, so it is no longer a safe runtime anchor for a re-run.
        _clear_runtime_anchor_evidence(copilot_ctx)

    # The kept prefix must still be a contiguous leading run of the new workflow;
    # a reorder or upstream insertion breaks that (set-membership trust is
    # order-blind), so fail closed when it no longer is.
    current_labels = _workflow_definition_block_labels(new_definition)
    remaining = list(copilot_ctx.verified_prefix_labels or [])
    if remaining and remaining != current_labels[: len(remaining)]:
        _reset_all_verified_trust(copilot_ctx)
        return

    # The end-to-end claim survives only an identical block list whose every block
    # is still verified, so append/removal/reorder/config edits all drop it.
    verified = set(remaining) | set(evidence.block_verified or [])
    if not (current_labels == prior_labels and all(label in verified for label in current_labels)):
        evidence.full_workflow_verified = False
        copilot_ctx.last_full_workflow_test_ok = False


def _nearest_upstream_state_establisher(
    requested_labels: list[str], target_label: str, new_definition: object | None
) -> str | None:
    by_label = _blocks_by_label(new_definition)
    try:
        idx = requested_labels.index(target_label)
    except ValueError:
        return None
    for candidate in reversed(requested_labels[:idx]):
        block = by_label.get(candidate)
        if block is None:
            continue
        if _block_type_name(block) in _BLOCK_TYPES_STATE_ESTABLISHER:
            return candidate
    return None


def _block_can_start_browser_run(block: object) -> bool:
    if _block_type_name(block) == BlockType.GOTO_URL.value:
        return True
    return _valid_runtime_anchor_url(getattr(block, "url", None)) is not None


def _nearest_upstream_runnable_anchor(
    workflow_labels: list[str], target_label: str, new_definition: object | None
) -> str | None:
    by_label = _blocks_by_label(new_definition)
    try:
        idx = workflow_labels.index(target_label)
    except ValueError:
        return None
    for candidate in reversed(workflow_labels[:idx]):
        block = by_label.get(candidate)
        if block is not None and _block_can_start_browser_run(block):
            return candidate
    return workflow_labels[0] if workflow_labels[:idx] else None


def _serialized_frontier_block_configs(frontier_labels: list[str], new_definition: object | None) -> list[str]:
    by_label = _blocks_by_label(new_definition)
    serialized_configs: list[str] = []
    for label in frontier_labels:
        block = by_label.get(label)
        if block is None:
            continue
        try:
            serialized_configs.append(json.dumps(_canonical_block_config(block), default=str, separators=(",", ":")))
        except (TypeError, ValueError):
            serialized_configs.append(repr(block))
    return serialized_configs


def _workflow_parameter_keys(definition: object | None) -> set[str]:
    parameters = getattr(definition, "parameters", None) if definition else None
    keys: set[str] = set()
    if not parameters:
        return keys
    for parameter in parameters:
        key = getattr(parameter, "key", None)
        if isinstance(key, str):
            keys.add(key)
    return keys


_CREDENTIAL_REAL_VALUE_SUFFIXES = ("_real_username", "_real_password")


def _classify_frontier_jinja_refs(
    frontier_labels: list[str],
    new_definition: object | None,
    serialized_configs: list[str] | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Single pass over frontier blocks; returns ``(suffix_form_refs, block_form_refs, unknown_roots)``."""
    if serialized_configs is None:
        serialized_configs = _serialized_frontier_block_configs(frontier_labels, new_definition)
    known_labels = set(_blocks_by_label(new_definition))
    parameter_keys = _workflow_parameter_keys(new_definition)
    known_roots = known_labels | parameter_keys | _TEMPLATE_BUILTIN_ROOTS

    suffix_form_refs: set[str] = set()
    block_form_refs: set[str] = set()
    unknown_roots: set[str] = set()

    for serialized in serialized_configs:
        for match in _OUTPUT_REF_RE.findall(serialized):
            if match in known_labels:
                suffix_form_refs.add(match)
        for match in _BLOCK_FORM_REF_RE.findall(serialized):
            if match in known_labels:
                block_form_refs.add(match)
        for root in _JINJA_ROOT_RE.findall(serialized):
            if root in known_roots:
                continue
            if root.endswith("_output") and root[: -len("_output")] in known_labels:
                continue
            if any(
                root.endswith(suffix) and root[: -len(suffix)] in parameter_keys
                for suffix in _CREDENTIAL_REAL_VALUE_SUFFIXES
            ):
                continue
            unknown_roots.add(root)

    return suffix_form_refs, block_form_refs, unknown_roots


def _referenced_output_labels(
    frontier_labels: list[str],
    new_definition: object | None,
    serialized_configs: list[str] | None = None,
) -> set[str]:
    suffix_refs, block_form_refs, _ = _classify_frontier_jinja_refs(frontier_labels, new_definition, serialized_configs)
    return suffix_refs | block_form_refs


def _block_form_output_labels(
    frontier_labels: list[str],
    new_definition: object | None,
    serialized_configs: list[str] | None = None,
) -> set[str]:
    _, block_form_refs, _ = _classify_frontier_jinja_refs(frontier_labels, new_definition, serialized_configs)
    return block_form_refs


def _unknown_jinja_roots(
    frontier_labels: list[str],
    new_definition: object | None,
    serialized_configs: list[str] | None = None,
) -> set[str]:
    _, _, unknown_roots = _classify_frontier_jinja_refs(frontier_labels, new_definition, serialized_configs)
    return unknown_roots


async def _get_prior_workflow_definition(ctx: AgentContext) -> object | None:
    """Hybrid: prefer ctx.last_workflow, fall back to DB fetch on cold start."""
    last_workflow = getattr(ctx, "last_workflow", None)
    if last_workflow is not None:
        definition = getattr(last_workflow, "workflow_definition", None)
        if definition is not None:
            return definition
    last_yaml = getattr(ctx, "last_workflow_yaml", None)
    if last_yaml:
        try:
            workflow = _process_workflow_yaml(
                workflow_id=ctx.workflow_id,
                workflow_permanent_id=ctx.workflow_permanent_id,
                organization_id=ctx.organization_id,
                workflow_yaml=last_yaml,
            )
            return workflow.workflow_definition
        except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException):
            pass
    try:
        fetched = await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
        )
        if fetched is not None:
            return fetched.workflow_definition
    except Exception:
        LOG.debug("Failed to fetch prior workflow definition for frontier diff", exc_info=True)
    return None


async def _get_prior_workflow(ctx: AgentContext) -> Workflow | None:
    """Return the prior Workflow; in-memory > re-parsed yaml > DB."""
    last_workflow = ctx.last_workflow
    if last_workflow is not None:
        return last_workflow
    last_yaml = ctx.last_workflow_yaml
    if last_yaml:
        try:
            return _process_workflow_yaml(
                workflow_id=ctx.workflow_id,
                workflow_permanent_id=ctx.workflow_permanent_id,
                organization_id=ctx.organization_id,
                workflow_yaml=last_yaml,
            )
        except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException):
            pass
    try:
        return await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
        )
    except Exception:
        LOG.warning(
            "Failed to fetch prior workflow for staging comparison; staging may skip a needed canonical write",
            exc_info=True,
        )
    return None


# Must stay in lockstep with the writers calling update_workflow_definition;
# missing fields silently drop accepted settings on auto-accept.
_CANONICAL_WORKFLOW_SETTING_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "proxy_location",
    "webhook_callback_url",
    "totp_verification_url",
    "totp_identifier",
    "persist_browser_session",
    "browser_profile_id",
    "model",
    "max_screenshot_scrolls",
    "extra_http_headers",
    "cdp_connect_headers",
    "run_with",
    "ai_fallback",
    "cache_key",
    "adaptive_caching",
    "code_version",
    "run_sequentially",
    "sequential_key",
)

# convert_workflow_definition regenerates ids/timestamps per call; ignore
# them when comparing parameters to react only to user intent.
_PARAMETER_FINGERPRINT_VOLATILE_KEYS = frozenset(
    {
        "workflow_parameter_id",
        "output_parameter_id",
        "aws_secret_parameter_id",
        "azure_secret_parameter_id",
        "azure_vault_credential_parameter_id",
        "bitwarden_credit_card_data_parameter_id",
        "bitwarden_login_credential_parameter_id",
        "bitwarden_sensitive_information_parameter_id",
        "credential_parameter_id",
        "onepassword_credential_parameter_id",
        "created_at",
        "modified_at",
    }
)


def _stable_parameter_fingerprint(parameter: Parameter) -> dict[str, Any]:
    dump = parameter.model_dump(mode="json")
    return {k: v for k, v in dump.items() if k not in _PARAMETER_FINGERPRINT_VOLATILE_KEYS}


def _workflow_requires_canonical_persist(prior: Workflow | None, new: Workflow) -> bool:
    if prior is None:
        return False
    for field_name in _CANONICAL_WORKFLOW_SETTING_FIELDS:
        if getattr(prior, field_name, None) != getattr(new, field_name, None):
            return True
    prior_params = prior.workflow_definition.parameters
    new_params = new.workflow_definition.parameters
    if len(prior_params) != len(new_params):
        return True
    prior_fingerprints = [_stable_parameter_fingerprint(p) for p in prior_params]
    new_fingerprints = [_stable_parameter_fingerprint(p) for p in new_params]
    return prior_fingerprints != new_fingerprints


def _plan_frontier(
    ctx: AgentContext,
    requested_labels: list[str],
    old_definition: object | None,
    new_definition: object | None,
) -> tuple[list[str], dict[str, Any], str | None]:
    """Plan the frontier execution.

    Returns ``(labels_to_execute, block_outputs_to_seed, frontier_start_label)``.

    Falls back to the full requested list on any ambiguity. When there is no
    workflow change (plain run path) the frontier is the first requested label,
    and we seed verified outputs referenced by the suffix plus prior
    browser-state outputs needed to start a downstream frontier.
    """
    if not requested_labels:
        return requested_labels, {}, None
    if new_definition is None:
        return requested_labels, {}, requested_labels[0]

    verified_outputs: dict[str, Any] = dict(ctx.verified_block_outputs or {})
    verified_prefix: list[str] = list(ctx.verified_prefix_labels or [])
    verified_prefix_set = set(verified_prefix)

    # No old definition (cold start or parse failure) OR no diff signal → plain path.
    if old_definition is None:
        frontier = requested_labels[0]
        return _seed_for_frontier(requested_labels, frontier, verified_outputs, new_definition)

    try:
        invalidated = _find_invalidated_labels(old_definition, new_definition, requested_labels)
    except Exception:
        LOG.debug("Frontier diff failed, falling back to full run", exc_info=True)
        return requested_labels, {}, requested_labels[0]

    earliest = _earliest_invalidated(requested_labels, invalidated)
    if earliest is None:
        # No invalidation at all — unchanged request. Continue from the
        # first unverified requested label so a model may keep passing the
        # complete chain while the tool advances the browser in small
        # verified frontiers.
        next_frontier = _first_unverified_requested_label(requested_labels, verified_prefix_set)
        if next_frontier is not None:
            return _seed_for_frontier(requested_labels, next_frontier, verified_outputs, new_definition)

        # If the model accidentally asks to rerun an already-verified prefix,
        # keep the browser moving forward instead of spending another tool call
        # on work the current session has already covered.
        workflow_labels = _workflow_definition_block_labels(new_definition)
        next_workflow_frontier = _first_unverified_requested_label(workflow_labels, verified_prefix_set)
        if next_workflow_frontier is not None:
            frontier_idx = workflow_labels.index(next_workflow_frontier)
            return _seed_for_frontier(
                workflow_labels[: frontier_idx + 1],
                next_workflow_frontier,
                verified_outputs,
                new_definition,
            )

        return _seed_for_frontier(requested_labels, requested_labels[0], verified_outputs, new_definition)

    # Ensure the prefix before the earliest invalidated label is all in the
    # verified prefix from a successful prior run. Otherwise we have no
    # trusted anchor — fall back to the full requested list.
    prefix_in_requested = [label for label in requested_labels if label != earliest]
    prefix_in_requested = prefix_in_requested[: requested_labels.index(earliest)]
    if not all(label in verified_prefix_set for label in prefix_in_requested):
        return requested_labels, {}, requested_labels[0]

    old_by_label = _blocks_by_label(old_definition)
    is_append_only = earliest not in old_by_label
    if is_append_only:
        # Case A — append-after-success. The earliest invalidated label is a
        # new block that didn't exist in the prior definition, so the verified
        # prefix represents the browser state just before it. Start there.
        workflow_labels = _workflow_definition_block_labels(new_definition)
        if earliest in workflow_labels:
            workflow_prefix = workflow_labels[: workflow_labels.index(earliest)]
            if not all(label in verified_prefix_set for label in workflow_prefix):
                anchor = _nearest_upstream_runnable_anchor(workflow_labels, earliest, new_definition)
                if anchor is not None:
                    return _seed_for_frontier(
                        workflow_labels[workflow_labels.index(anchor) : workflow_labels.index(earliest) + 1],
                        anchor,
                        verified_outputs,
                        new_definition,
                    )
        return _seed_for_frontier(requested_labels, earliest, verified_outputs, new_definition)

    # Edit-in-place. We lack a browser-anchor signal, so we cannot safely
    # rerun just the edited block (the browser is at post-prefix state, not
    # pre-edit state). Walk back to the nearest upstream state-establishing
    # block within the requested chain. Falls back to the full requested list
    # if no safe upstream anchor can be identified.
    anchor = _nearest_upstream_state_establisher(requested_labels, earliest, new_definition)
    if anchor is None:
        return requested_labels, {}, requested_labels[0]
    return _seed_for_frontier(requested_labels, anchor, verified_outputs, new_definition)


def _first_unverified_requested_label(requested_labels: list[str], verified_prefix_set: set[str]) -> str | None:
    for label in requested_labels:
        if label not in verified_prefix_set:
            return label
    return None


def _seed_for_frontier(
    requested_labels: list[str],
    frontier: str,
    verified_outputs: dict[str, Any],
    new_definition: object | None,
) -> tuple[list[str], dict[str, Any], str]:
    try:
        idx = requested_labels.index(frontier)
    except ValueError:
        return requested_labels, {}, requested_labels[0]
    labels_to_execute = requested_labels[idx:]
    workflow_labels = _workflow_definition_block_labels(new_definition)
    if frontier in workflow_labels:
        prefix_labels = workflow_labels[: workflow_labels.index(frontier)]
    else:
        prefix_labels = requested_labels[:idx]
    if not prefix_labels:
        return labels_to_execute, {}, frontier
    serialized_configs = _serialized_frontier_block_configs(labels_to_execute, new_definition)
    suffix_refs, block_form_refs, unknown_roots = _classify_frontier_jinja_refs(
        labels_to_execute, new_definition, serialized_configs
    )
    if any(label in block_form_refs for label in prefix_labels):
        # Seeded block_outputs only register <label>_output; block-form refs
        # need a normal upstream execution to populate the <label> namespace.
        return requested_labels, {}, requested_labels[0]
    needed = suffix_refs | block_form_refs
    seed: dict[str, Any] = {}
    for label in prefix_labels:
        if label not in needed:
            continue
        if label not in verified_outputs:
            return requested_labels, {}, requested_labels[0]
        seed[label] = verified_outputs[label]
    if unknown_roots:
        return requested_labels, {}, requested_labels[0]
    by_label = _blocks_by_label(new_definition)
    for label in prefix_labels:
        block = by_label.get(label)
        if block is None or _block_type_name(block) not in _BLOCK_TYPES_STATE_ESTABLISHER:
            continue
        if label in verified_outputs:
            seed.setdefault(label, verified_outputs[label])
    return labels_to_execute, seed, frontier


_MAX_INCREMENTAL_PAGE_FRONTIER_LABELS = 2
_PAGE_CHANGING_FRONTIER_BLOCK_TYPES: frozenset[str] = frozenset(
    {
        BlockType.ACTION.value,
        BlockType.FILE_DOWNLOAD.value,
        BlockType.FILE_UPLOAD.value,
        BlockType.LOGIN.value,
        BlockType.NAVIGATION.value,
    }
)


def _frontier_block_type_names(labels: list[str], workflow_definition: object | None) -> list[str]:
    by_label = _blocks_by_label(workflow_definition)
    type_names: list[str] = []
    for label in labels:
        block = by_label.get(label)
        if block is None:
            continue
        type_name = _block_type_name(block)
        if type_name:
            type_names.append(type_name)
    return type_names


def _frontier_has_several_page_changing_stages(labels: list[str], workflow_definition: object | None) -> bool:
    type_names = _frontier_block_type_names(labels, workflow_definition)
    if len(type_names) <= _MAX_INCREMENTAL_PAGE_FRONTIER_LABELS:
        return False
    page_changing_count = sum(1 for type_name in type_names if type_name in _PAGE_CHANGING_FRONTIER_BLOCK_TYPES)
    return page_changing_count >= 2 or (page_changing_count >= 1 and len(type_names) >= 4)


def _frontier_includes_required_runtime_anchor(block_labels: list[str], labels_to_execute: list[str]) -> bool:
    if not block_labels or len(labels_to_execute) <= len(block_labels):
        return False
    return labels_to_execute[-len(block_labels) :] == block_labels


def _frontier_run_size_error(
    copilot_ctx: object,
    block_labels: list[str],
    labels_to_execute: list[str],
    workflow_definition: object | None,
) -> str | None:
    if len(labels_to_execute) <= _MAX_INCREMENTAL_PAGE_FRONTIER_LABELS:
        return None
    if _frontier_includes_required_runtime_anchor(block_labels, labels_to_execute):
        return None
    if getattr(copilot_ctx, "build_phase", None) not in (BuildPhase.COMPOSING, BuildPhase.TESTING):
        return None
    if not _frontier_has_several_page_changing_stages(labels_to_execute, workflow_definition):
        return None

    suggested = labels_to_execute[:_MAX_INCREMENTAL_PAGE_FRONTIER_LABELS]
    remaining = labels_to_execute[_MAX_INCREMENTAL_PAGE_FRONTIER_LABELS:]
    return (
        "Workflow validation failed: this browser test frontier is too long for a multi-stage "
        "page-changing workflow. Keep the same complete workflow YAML, but shrink only the "
        f"block_labels argument to the next 1-2 unverified labels: {suggested!r}. "
        "If a prior run already advanced the browser, inspect that reached page "
        '(inspect_page_for_composition(target_url="current_page")) to ground the next labels in '
        "what is actually there rather than shrinking the frontier blind. "
        f"Do not remove later blocks from the YAML; test them after this frontier succeeds. "
        f"Deferred labels: {remaining!r}. Requested labels: {block_labels!r}."
    )


def _workflow_with_runtime_block_goal_context(workflow: Workflow, ctx: CopilotContext) -> Workflow:
    block_goal_main_goal = ctx.block_goal_main_goal or ctx.user_message or ""
    if not block_goal_main_goal:
        LOG.warning("run_blocks invoked without block-goal context; using persisted workflow goals unchanged")
        return workflow
    return wrap_workflow_block_goals(workflow, block_goal_main_goal)


def _blank_runtime_page_url(value: object) -> bool:
    if not isinstance(value, str):
        return True
    # Chrome/CDP can expose ":" while the page is still in early blank-page initialization.
    return value.strip() in {"about:blank", "", ":"}


def _missing_runtime_frontier_block_url(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _same_runtime_page(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        left_parsed = urlparse(left)
        right_parsed = urlparse(right)
    except ValueError:
        return False
    return (
        left_parsed.scheme.lower(),
        left_parsed.netloc.lower(),
        left_parsed.path,
        left_parsed.query,
    ) == (
        right_parsed.scheme.lower(),
        right_parsed.netloc.lower(),
        right_parsed.path,
        right_parsed.query,
    )


def _frontier_anchor_url_from_value(value: object, *, depth: int = 0) -> str | None:
    if depth > 3:
        return None
    direct = _valid_runtime_anchor_url(value)
    if direct is not None:
        return direct
    if isinstance(value, dict):
        for key in ("current_url", "url", "page_url"):
            candidate = _valid_runtime_anchor_url(value.get(key))
            if candidate is not None:
                return candidate
        for nested in value.values():
            candidate = _frontier_anchor_url_from_value(nested, depth=depth + 1)
            if candidate is not None:
                return candidate
    if isinstance(value, list):
        for nested in value:
            candidate = _frontier_anchor_url_from_value(nested, depth=depth + 1)
            if candidate is not None:
                return candidate
    return None


def _frontier_runtime_anchor_url(ctx: CopilotContext, block_outputs_to_seed: dict[str, Any]) -> str | None:
    for value in (
        ctx.verified_prefix_current_url,
        getattr(ctx, "composition_page_evidence", None),
    ):
        candidate = _frontier_anchor_url_from_value(value)
        if candidate is not None:
            return candidate
    for value in reversed(list((block_outputs_to_seed or {}).values())):
        candidate = _frontier_anchor_url_from_value(value)
        if candidate is not None:
            return candidate
    return None


def _iter_workflow_model_blocks(blocks: list[BlockTypeVar] | None) -> list[BlockTypeVar]:
    if not isinstance(blocks, list):
        return []
    return get_all_blocks(blocks)


def _workflow_model_block_by_label(workflow_definition: object | None, label: str | None) -> BlockTypeVar | None:
    if workflow_definition is None or not label:
        return None
    blocks = workflow_definition.blocks if hasattr(workflow_definition, "blocks") else None
    for block in _iter_workflow_model_blocks(blocks):
        if block.label == label:
            return block
    return None


def _has_verified_prefix_before_frontier(
    ctx: CopilotContext, workflow_definition: object | None, frontier_label: str | None
) -> bool:
    if not frontier_label:
        return False
    workflow_labels = _workflow_definition_block_labels(workflow_definition)
    if frontier_label not in workflow_labels:
        return False
    prefix_labels = workflow_labels[: workflow_labels.index(frontier_label)]
    if not prefix_labels:
        return False
    verified = set(ctx.verified_prefix_labels or [])
    return all(label in verified for label in prefix_labels)


def _workflow_with_runtime_frontier_anchor(
    workflow: Workflow,
    ctx: CopilotContext,
    *,
    labels_to_execute: list[str],
    frontier_start_label: str | None,
    block_outputs_to_seed: dict[str, Any],
) -> tuple[Workflow, str | None]:
    if not labels_to_execute:
        return workflow, None
    workflow_definition = workflow.workflow_definition
    if not _has_verified_prefix_before_frontier(ctx, workflow_definition, frontier_start_label):
        return workflow, None

    first_label = labels_to_execute[0]
    first_block = _workflow_model_block_by_label(workflow_definition, first_label)
    if first_block is None or not hasattr(first_block, "url"):
        return workflow, None

    anchor_url = _frontier_runtime_anchor_url(ctx, block_outputs_to_seed)
    if anchor_url is None:
        return workflow, None

    existing_url = _valid_runtime_anchor_url(first_block.url if hasattr(first_block, "url") else None)
    if existing_url is not None and not _same_runtime_page(existing_url, anchor_url):
        return workflow, None

    if existing_url is not None:
        if _block_type_name(first_block) != BlockType.NAVIGATION.value:
            return workflow, None
        anchored = workflow.model_copy(deep=True)
        anchored_block = _workflow_model_block_by_label(anchored.workflow_definition, first_label)
        if anchored_block is None or not hasattr(anchored_block, "url"):
            return workflow, None
        anchored_block.url = None
        LOG.info(
            "Cleared runtime frontier URL to preserve browser state",
            frontier_start_label=frontier_start_label,
            first_block_label=first_label,
            existing_url=existing_url,
            continuation_url=anchor_url,
        )
        return anchored, anchor_url

    LOG.info(
        "Preserved runtime frontier browser state without URL reload",
        frontier_start_label=frontier_start_label,
        first_block_label=first_label,
        continuation_url=anchor_url,
    )
    return workflow, anchor_url


async def _workflow_with_runtime_frontier_starter_url_seed(
    workflow: Workflow,
    ctx: CopilotContext,
    *,
    labels_to_execute: list[str],
    runtime_frontier_anchor_url: str | None,
) -> Workflow:
    if not labels_to_execute or runtime_frontier_anchor_url is None or not ctx.browser_session_id:
        return workflow

    first_label = labels_to_execute[0]
    first_block = _workflow_model_block_by_label(workflow.workflow_definition, first_label)
    if (
        first_block is None
        or not hasattr(first_block, "url")
        or _block_type_name(first_block) != BlockType.NAVIGATION.value
        or not _missing_runtime_frontier_block_url(first_block.url)
    ):
        return workflow

    current_page_url: str | None = None
    try:
        browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
            session_id=ctx.browser_session_id,
            organization_id=ctx.organization_id,
        )
        if browser_state is not None:
            page = await browser_state.get_working_page()
            # Playwright Page.url is exposed as a dynamic property at this boundary.
            current_page_url = page.url if page is not None else None
    except Exception:
        LOG.debug(
            "Failed to inspect runtime frontier browser page before starter URL seed",
            browser_session_id=ctx.browser_session_id,
            frontier_start_label=first_label,
            exc_info=True,
        )

    if not _blank_runtime_page_url(current_page_url):
        LOG.info(
            "Preserved attached runtime frontier browser page",
            browser_session_id=ctx.browser_session_id,
            frontier_start_label=first_label,
            current_url=current_page_url,
            continuation_url=runtime_frontier_anchor_url,
        )
        return workflow

    seeded = workflow.model_copy(deep=True)
    seeded_block = _workflow_model_block_by_label(seeded.workflow_definition, first_label)
    # Defensive: the copied workflow definition should preserve labels and URL fields.
    if seeded_block is None or not hasattr(seeded_block, "url"):
        return workflow
    seeded_block.url = runtime_frontier_anchor_url
    LOG.info(
        "Seeded runtime frontier starter URL because attached browser page was blank",
        browser_session_id=ctx.browser_session_id,
        frontier_start_label=first_label,
        current_url=current_page_url,
        continuation_url=runtime_frontier_anchor_url,
    )
    return seeded
