"""Copilot workflow-YAML normalization, chain repair, and Workflow conversion."""

from collections.abc import Collection
from datetime import datetime, timezone
from typing import Any

import structlog
import yaml

from skyvern.constants import DEFAULT_LOGIN_PROMPT
from skyvern.exceptions import WorkflowNotFound
from skyvern.forge import app
from skyvern.forge.sdk.copilot.block_type_aliases import normalize_copilot_block_type_alias
from skyvern.forge.sdk.copilot.code_block_steps import derive_code_block_steps_in_yaml
from skyvern.forge.sdk.workflow.models.parameter import ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.runs import ProxyLocation
from skyvern.schemas.workflows import (
    BlockYAML,
    BranchConditionYAML,
    ConditionalBlockYAML,
    ForLoopBlockYAML,
    LoginBlockYAML,
    WhileLoopBlockYAML,
    WorkflowCreateYAMLRequest,
)
from skyvern.utils.yaml_loader import safe_load_no_dates

LOG = structlog.get_logger()


def _proxy_location_alias_key(value: str) -> str:
    return "_".join(value.strip().upper().replace("-", "_").split())


def _build_copilot_proxy_location_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}

    def add(alias: str, proxy_location: ProxyLocation) -> None:
        aliases[_proxy_location_alias_key(alias)] = proxy_location.value

    for proxy_location in ProxyLocation:
        add(proxy_location.name, proxy_location)
        add(proxy_location.value, proxy_location)

    for proxy_location in ProxyLocation.residential_country_locations():
        add(ProxyLocation.get_country_code(proxy_location), proxy_location)

    add("USA", ProxyLocation.RESIDENTIAL)
    add("United States", ProxyLocation.RESIDENTIAL)
    add("United States of America", ProxyLocation.RESIDENTIAL)
    add("RESIDENTIAL_US", ProxyLocation.RESIDENTIAL)
    add("UK", ProxyLocation.RESIDENTIAL_GB)
    add("United Kingdom", ProxyLocation.RESIDENTIAL_GB)

    return aliases


_COPILOT_PROXY_LOCATION_ALIASES = _build_copilot_proxy_location_aliases()


def _canonicalize_copilot_proxy_location(parsed_yaml: dict[str, Any]) -> None:
    if "proxy_location" not in parsed_yaml:
        return

    proxy_location = parsed_yaml.get("proxy_location")
    if not isinstance(proxy_location, str):
        return

    canonical = _COPILOT_PROXY_LOCATION_ALIASES.get(_proxy_location_alias_key(proxy_location))
    if canonical is None:
        return

    parsed_yaml["proxy_location"] = canonical


def _canonicalize_copilot_block_type_aliases(value: Any) -> None:
    if isinstance(value, dict):
        block_type = value.get("block_type")
        if isinstance(block_type, str):
            value["block_type"] = normalize_copilot_block_type_alias(block_type)
        for child in value.values():
            _canonicalize_copilot_block_type_aliases(child)
    elif isinstance(value, list):
        for item in value:
            _canonicalize_copilot_block_type_aliases(item)


def _collect_reachable(
    start_label: str,
    label_to_block: dict[str, BlockYAML],
    reachable: set[str],
) -> None:
    """Walk the next_block_label chain from start_label, collecting all reachable labels.

    For conditional blocks, also follows branch target chains recursively.

    The ``current not in reachable`` loop guard means the main-chain walk
    stops early if we hit a node already collected via a branch recursion.
    This is correct — those downstream nodes and their successors are
    already in ``reachable`` — but callers should be aware of the coupling.
    """
    current: str | None = start_label
    while current and current in label_to_block and current not in reachable:
        reachable.add(current)
        block = label_to_block[current]
        if isinstance(block, ConditionalBlockYAML):
            for branch in block.branch_conditions:
                if branch.next_block_label and branch.next_block_label not in reachable:
                    _collect_reachable(branch.next_block_label, label_to_block, reachable)
        current = block.next_block_label


def _break_cycles(
    start_label: str,
    label_to_block: dict[str, BlockYAML],
) -> bool:
    """Detect and break circular references in the block chain using DFS.

    Uses a recursion stack to distinguish true back-edges (cycles) from merge
    points (two branches converging on the same block).  When a back-edge is
    found the offending ``next_block_label`` is set to ``None``, breaking the
    cycle.  Handles both the main chain and conditional branch chains.

    Note: this function operates on a single level of blocks.  It does **not**
    recurse into ``ForLoopBlockYAML.loop_blocks``; nested loops are handled
    by the recursive ``_repair_next_block_label_chain`` call in Phase 3.

    Returns True if at least one cycle was broken.
    """
    visited: set[str] = set()
    rec_stack: set[str] = set()
    found_cycle = False

    def _follow_edge(target: str | None, edge_owner: BlockYAML | BranchConditionYAML, parent_label: str) -> None:
        """Follow an edge to *target*.  *edge_owner* is the object whose
        ``next_block_label`` will be set to ``None`` when the target forms a
        back-edge.  *parent_label* is the block label that owns this edge
        for logging."""
        nonlocal found_cycle
        if not target or target not in label_to_block:
            return
        if target in rec_stack:
            is_branch = hasattr(edge_owner, "criteria")
            LOG.warning(
                "Copilot produced circular block chain, breaking cycle",
                cycle_target=target,
                broken_at=parent_label,
                is_branch_condition=is_branch,
                branch_expression=getattr(getattr(edge_owner, "criteria", None), "expression", None),
            )
            edge_owner.next_block_label = None
            found_cycle = True
            return
        if target in visited:
            return  # merge point — not a cycle
        _dfs(target)

    def _dfs(label: str) -> None:
        visited.add(label)
        rec_stack.add(label)
        block = label_to_block[label]

        if isinstance(block, ConditionalBlockYAML):
            for branch in block.branch_conditions:
                _follow_edge(branch.next_block_label, branch, label)

        _follow_edge(block.next_block_label, block, label)
        rec_stack.discard(label)

    if start_label in label_to_block:
        _dfs(start_label)
    return found_cycle


def _find_terminal_label(
    start_label: str,
    label_to_block: dict[str, BlockYAML],
    all_labels: set[str],
) -> str | None:
    """Find the terminal block by walking the main chain from start_label."""
    visited: set[str] = set()
    current: str | None = start_label
    while current and current in label_to_block and current not in visited:
        visited.add(current)
        block = label_to_block[current]
        if block.next_block_label is None or block.next_block_label not in all_labels:
            return current
        current = block.next_block_label
    return None


def _order_orphaned_blocks(
    orphaned_labels: set[str],
    label_to_block: dict[str, BlockYAML],
    all_labels: set[str],
    blocks: list[BlockYAML],
) -> list[str]:
    """Order orphaned blocks by following their internal next_block_label chains.

    Multiple disconnected orphan sub-chains are concatenated in the order their
    chain-start appears in the original blocks list.
    """
    pointed_to: set[str] = set()
    for label in orphaned_labels:
        block = label_to_block[label]
        if block.next_block_label and block.next_block_label in orphaned_labels:
            pointed_to.add(block.next_block_label)

    # Chain starts are orphans not pointed to by another orphan.
    # Preserve original array order for deterministic stitching.
    chain_starts = [b.label for b in blocks if b.label in orphaned_labels and b.label not in pointed_to]

    # If all orphans point to each other (cycle), pick the first in array order.
    if not chain_starts:
        chain_starts = [next(b.label for b in blocks if b.label in orphaned_labels)]

    ordered: list[str] = []
    visited: set[str] = set()
    for start in chain_starts:
        current: str | None = start
        while current and current in orphaned_labels and current not in visited:
            visited.add(current)
            ordered.append(current)
            current = label_to_block[current].next_block_label

    # Append any remaining orphans not reached (multiple cycles).
    for block in blocks:
        if block.label in orphaned_labels and block.label not in visited:
            ordered.append(block.label)

    # Re-link the orphan chain so it forms a single connected path.
    # This may overwrite an orphan's original next_block_label that pointed to a
    # reachable block (a merge/join pattern).  Log when this happens.
    for i in range(len(ordered) - 1):
        old_target = label_to_block[ordered[i]].next_block_label
        new_target = ordered[i + 1]
        if old_target and old_target != new_target and old_target not in orphaned_labels:
            LOG.info(
                "Orphan re-link overwrites cross-chain reference",
                block=ordered[i],
                old_target=old_target,
                new_target=new_target,
            )
        label_to_block[ordered[i]].next_block_label = new_target
    if ordered:
        old_last_target = label_to_block[ordered[-1]].next_block_label
        if old_last_target and old_last_target not in orphaned_labels:
            LOG.info(
                "Orphan chain terminal overwrites cross-chain reference",
                block=ordered[-1],
                old_target=old_last_target,
            )
        label_to_block[ordered[-1]].next_block_label = None

    return ordered


def _repair_next_block_label_chain(blocks: list[BlockYAML]) -> None:
    """Ensure all top-level blocks form a single acyclic chain from blocks[0].

    Repairs two classes of LLM mistakes:
    1. Circular references — breaks cycles so the chain has a proper terminal block.
    2. Disconnected paths — stitches orphaned blocks onto the end of the reachable chain.

    Recursively repairs nested loop block ``loop_blocks`` at all depths.
    Mutates *blocks* in place.
    """
    if len(blocks) <= 1:
        # Still recurse into loop_blocks even for single-block lists
        for block in blocks:
            if isinstance(block, (ForLoopBlockYAML, WhileLoopBlockYAML)) and block.loop_blocks:
                _repair_next_block_label_chain(block.loop_blocks)
        return

    # Warn on duplicate labels — the dict comprehension silently keeps the last
    # occurrence, so earlier blocks with the same label become invisible.
    seen_labels: set[str] = set()
    for block in blocks:
        if block.label in seen_labels:
            LOG.warning("Copilot produced duplicate block label", label=block.label)
        seen_labels.add(block.label)

    label_to_block: dict[str, BlockYAML] = {block.label: block for block in blocks}
    all_labels = set(label_to_block.keys())

    # Phase 1: break any circular references reachable from the first block.
    # Note: cycles among orphaned blocks (unreachable from blocks[0]) are handled
    # implicitly by _order_orphaned_blocks via its visited set and re-linking logic.
    _break_cycles(blocks[0].label, label_to_block)

    # Phase 2: find orphaned (unreachable) blocks and stitch them to the end.
    reachable: set[str] = set()
    _collect_reachable(blocks[0].label, label_to_block, reachable)

    orphaned_labels = all_labels - reachable
    if orphaned_labels:
        LOG.warning(
            "Copilot produced disconnected workflow blocks, repairing chain",
            orphaned_labels=sorted(orphaned_labels),
            reachable_labels=sorted(reachable),
        )

        terminal_label = _find_terminal_label(blocks[0].label, label_to_block, all_labels)
        ordered_orphan_labels = _order_orphaned_blocks(orphaned_labels, label_to_block, all_labels, blocks)

        if terminal_label and ordered_orphan_labels:
            label_to_block[terminal_label].next_block_label = ordered_orphan_labels[0]

    # Phase 3: recursively repair nested loop block ``loop_blocks``.
    for block in blocks:
        if isinstance(block, (ForLoopBlockYAML, WhileLoopBlockYAML)) and block.loop_blocks:
            _repair_next_block_label_chain(block.loop_blocks)


def _normalize_copilot_yaml(workflow_yaml: str) -> WorkflowCreateYAMLRequest:
    parsed_yaml = safe_load_no_dates(workflow_yaml)

    # Fixing trivial common LLM mistakes; non-dict YAML falls through to model_validate.
    if isinstance(parsed_yaml, dict):
        # title is schema-required; coerce rather than force a self-healing round-trip.
        parsed_yaml.setdefault("title", "")
        _canonicalize_copilot_proxy_location(parsed_yaml)
        workflow_definition = parsed_yaml.get("workflow_definition", None)
        if workflow_definition:
            _canonicalize_copilot_block_type_aliases(workflow_definition)
            blocks = workflow_definition.get("blocks", []) or []
            for block in blocks:
                block["title"] = block.get("title", "")

    workflow_yaml_request = WorkflowCreateYAMLRequest.model_validate(parsed_yaml)

    # Post-processing
    for block in workflow_yaml_request.workflow_definition.blocks:
        if isinstance(block, LoginBlockYAML) and not block.navigation_goal:
            block.navigation_goal = DEFAULT_LOGIN_PROMPT

    workflow_yaml_request.workflow_definition.parameters = [
        p for p in workflow_yaml_request.workflow_definition.parameters if p.parameter_type != ParameterType.OUTPUT
    ]

    _repair_next_block_label_chain(workflow_yaml_request.workflow_definition.blocks)

    return workflow_yaml_request


def _yaml_bool_setting(workflow_yaml: str | None, setting_name: str) -> bool | None:
    if not workflow_yaml:
        return None
    try:
        parsed = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get(setting_name)
    return value if isinstance(value, bool) else None


def _yaml_enable_self_healing(workflow_yaml: str | None) -> bool | None:
    return _yaml_bool_setting(workflow_yaml, "enable_self_healing")


def _yaml_pin_saved_session_ip(workflow_yaml: str | None) -> bool | None:
    return _yaml_bool_setting(workflow_yaml, "pin_saved_session_ip")


def _redact_credentials_before_persistence(
    workflow_yaml: str, workflow_permanent_id: str, credential_values: Collection[str]
) -> str:
    """Last line of defence before a workflow reaches the database.

    A credential value should already have been rebound to a parameter reference upstream; reaching
    here with a live value means that failed, so redact rather than store it. The redacted workflow
    will not run as authored — that is the intended trade, and the error line is the signal to fix
    the upstream rebind.

    ``credential_values`` must be scoped to the authoring session. Replacing values registered by
    other sessions would let one org's short secret rewrite another org's stored workflow.
    """
    from skyvern.forge.sdk.copilot.secret_scrub import MIN_PERSISTED_REDACTION_LENGTH, REDACTED_SECRET_PLACEHOLDER

    redactable = {
        value for value in credential_values if isinstance(value, str) and len(value) >= MIN_PERSISTED_REDACTION_LENGTH
    }
    redacted_count = 0
    # Longest first so an overlapping shorter value never splits a longer one.
    for secret in sorted(redactable, key=len, reverse=True):
        if secret in workflow_yaml:
            redacted_count += workflow_yaml.count(secret)
            workflow_yaml = workflow_yaml.replace(secret, REDACTED_SECRET_PLACEHOLDER)
    if redacted_count:
        LOG.error(
            "copilot redacted a raw credential value at the workflow persistence seam",
            workflow_permanent_id=workflow_permanent_id,
            redacted_occurrences=redacted_count,
        )
    return workflow_yaml


async def _process_workflow_yaml(
    workflow_id: str,
    workflow_permanent_id: str,
    organization_id: str,
    workflow_yaml: str,
    settings_fallback_yaml: str | None = None,
    settings_fallback_workflow: Workflow | None = None,
    credential_scrub_values: Collection[str] = (),
) -> Workflow:
    # Single seam every copilot YAML->Workflow conversion passes through, so code
    # blocks get their plain-view steps regardless of which path produced the YAML
    # (the update_workflow tool derives them upstream; the inline REPLACE_WORKFLOW
    # fallbacks would otherwise surface "No steps yet").
    workflow_yaml = _redact_credentials_before_persistence(
        workflow_yaml, workflow_permanent_id, credential_scrub_values
    )
    workflow_yaml = derive_code_block_steps_in_yaml(workflow_yaml)
    workflow_yaml_request = _normalize_copilot_yaml(workflow_yaml)

    updated_workflow_definition = convert_workflow_definition(
        workflow_definition_yaml=workflow_yaml_request.workflow_definition,
        workflow_id=workflow_id,
    )

    enable_self_healing = workflow_yaml_request.enable_self_healing
    if enable_self_healing is None:
        # Copilot YAML routinely omits settings it didn't touch; omission must inherit —
        # the canonical-persist comparison would otherwise read the schema default as an
        # explicit disable. The submitted draft YAML wins over persisted state so an
        # unsaved editor toggle survives an unrelated copilot edit. A persisted-lookup
        # failure propagates: failing the save is safer than writing an implicit disable.
        enable_self_healing = _yaml_enable_self_healing(settings_fallback_yaml)

    mask_secrets = workflow_yaml_request.mask_secrets
    if mask_secrets is None:
        mask_secrets = _yaml_bool_setting(settings_fallback_yaml, "mask_secrets")

    pin_saved_session_ip = _yaml_pin_saved_session_ip(workflow_yaml)
    if pin_saved_session_ip is None:
        pin_saved_session_ip = _yaml_pin_saved_session_ip(settings_fallback_yaml)

    if enable_self_healing is None:
        fallback_value = getattr(settings_fallback_workflow, "enable_self_healing", None)
        enable_self_healing = fallback_value if isinstance(fallback_value, bool) else None
    if mask_secrets is None:
        fallback_value = getattr(settings_fallback_workflow, "mask_secrets", None)
        mask_secrets = fallback_value if isinstance(fallback_value, bool) else None
    if pin_saved_session_ip is None:
        fallback_value = getattr(settings_fallback_workflow, "pin_saved_session_ip", None)
        pin_saved_session_ip = fallback_value if isinstance(fallback_value, bool) else None

    current_workflow = settings_fallback_workflow
    if enable_self_healing is None or mask_secrets is None or pin_saved_session_ip is None:
        try:
            current_workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
        except WorkflowNotFound:
            current_workflow = None
    if enable_self_healing is None:
        enable_self_healing = bool(current_workflow and getattr(current_workflow, "enable_self_healing", False))
    if mask_secrets is None:
        mask_secrets = bool(current_workflow and getattr(current_workflow, "mask_secrets", False))
    if pin_saved_session_ip is None:
        pin_saved_session_ip = bool(current_workflow and getattr(current_workflow, "pin_saved_session_ip", False))

    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id=workflow_id,
        organization_id=organization_id,
        title=workflow_yaml_request.title or "",
        workflow_permanent_id=workflow_permanent_id,
        version=1,
        is_saved_task=workflow_yaml_request.is_saved_task,
        description=workflow_yaml_request.description,
        workflow_definition=updated_workflow_definition,
        proxy_location=workflow_yaml_request.proxy_location,
        webhook_callback_url=workflow_yaml_request.webhook_callback_url,
        totp_verification_url=workflow_yaml_request.totp_verification_url,
        totp_identifier=workflow_yaml_request.totp_identifier,
        persist_browser_session=workflow_yaml_request.persist_browser_session or False,
        mask_secrets=mask_secrets,
        pin_saved_session_ip=pin_saved_session_ip,
        browser_profile_id=workflow_yaml_request.browser_profile_id,
        browser_profile_key=workflow_yaml_request.browser_profile_key,
        model=workflow_yaml_request.model,
        max_screenshot_scrolls=workflow_yaml_request.max_screenshot_scrolls,
        extra_http_headers=workflow_yaml_request.extra_http_headers,
        cdp_connect_headers=workflow_yaml_request.cdp_connect_headers,
        run_with=workflow_yaml_request.run_with,
        ai_fallback=workflow_yaml_request.ai_fallback,
        cache_key=workflow_yaml_request.cache_key,
        adaptive_caching=workflow_yaml_request.adaptive_caching,
        enable_self_healing=enable_self_healing,
        code_version=workflow_yaml_request.code_version,
        run_sequentially=workflow_yaml_request.run_sequentially,
        sequential_key=workflow_yaml_request.sequential_key,
        created_at=now,
        modified_at=now,
    )


class BlockEditError(Exception):
    """A block-scoped edit that could not be applied as asked."""


def _workflow_blocks(parsed: Any) -> list[Any] | None:
    if not isinstance(parsed, dict):
        return None
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return None
    blocks = definition.get("blocks")
    return blocks if isinstance(blocks, list) else None


def _block_by_label(blocks: list[Any], label: str) -> dict[str, Any]:
    matches = [b for b in blocks if isinstance(b, dict) and str(b.get("label") or "") == label]
    if not matches:
        known = ", ".join(sorted(str(b.get("label") or "") for b in blocks if isinstance(b, dict)))
        raise BlockEditError(f"No block labelled {label!r}. The workflow has: {known or '(no labelled blocks)'}.")
    if len(matches) > 1:
        raise BlockEditError(f"{len(matches)} blocks share the label {label!r}; labels must be unique to edit one.")
    return matches[0]


def apply_block_edit(
    stored_yaml: str,
    label: str,
    *,
    expected_code: str | None = None,
    replacement_code: str | None = None,
    fields: dict[str, Any] | None = None,
) -> str:
    """Apply an edit to one block and return the whole workflow, leaving every other block untouched.

    A code edit is anchored: ``expected_code`` must appear exactly once in the block's current code, so
    an edit written against a stale copy fails loudly instead of overwriting whatever is there now.
    """
    try:
        parsed = safe_load_no_dates(stored_yaml)
    except Exception as exc:
        raise BlockEditError(f"The stored workflow is not parseable: {exc}") from exc
    blocks = _workflow_blocks(parsed)
    if blocks is None:
        raise BlockEditError("The stored workflow has no workflow_definition.blocks to edit.")
    block = _block_by_label(blocks, label)

    if expected_code is not None or replacement_code is not None:
        if expected_code is None or replacement_code is None:
            raise BlockEditError("A code edit needs both expected_code and replacement_code.")
        current = block.get("code")
        if not isinstance(current, str):
            raise BlockEditError(f"Block {label!r} has no code to edit; use fields for its settings.")
        occurrences = current.count(expected_code)
        if occurrences == 0:
            raise BlockEditError(
                f"expected_code was not found in block {label!r}. It has changed since you read it. "
                f"Its code is now:\n{current}\n"
                "Rewrite the edit against exactly that text."
            )
        if occurrences > 1:
            raise BlockEditError(
                f"expected_code appears {occurrences} times in block {label!r}; include enough "
                f"surrounding lines to identify one occurrence. Its code is now:\n{current}"
            )
        block["code"] = current.replace(expected_code, replacement_code, 1)

    for key, value in (fields or {}).items():
        block[key] = value

    return yaml.safe_dump(parsed, sort_keys=False)


def delete_block_from_workflow(stored_yaml: str, label: str) -> str:
    """Remove one block by label and return the whole workflow.

    Deletion is an operation rather than an absence, so a submission that simply omits a block can
    never be mistaken for a request to remove it.
    """
    try:
        parsed = safe_load_no_dates(stored_yaml)
    except Exception as exc:
        raise BlockEditError(f"The stored workflow is not parseable: {exc}") from exc
    blocks = _workflow_blocks(parsed)
    if blocks is None:
        raise BlockEditError("The stored workflow has no workflow_definition.blocks to edit.")
    _block_by_label(blocks, label)
    remaining = [b for b in blocks if not (isinstance(b, dict) and str(b.get("label") or "") == label)]
    for other in remaining:
        if isinstance(other, dict) and other.get("next_block_label") == label:
            other["next_block_label"] = None
    parsed["workflow_definition"]["blocks"] = remaining
    return yaml.safe_dump(parsed, sort_keys=False)
