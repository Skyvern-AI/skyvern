"""Copilot workflow-YAML normalization, chain repair, and Workflow conversion."""

from collections.abc import Collection
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import structlog
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from skyvern.constants import DEFAULT_LOGIN_PROMPT, DEFAULT_WORKFLOW_TITLES
from skyvern.exceptions import WorkflowNotFound
from skyvern.forge import app
from skyvern.forge.sdk.copilot.block_type_aliases import normalize_copilot_block_type_alias
from skyvern.forge.sdk.copilot.code_block_steps import (
    bind_referenced_parameters_in_yaml,
    derive_code_block_steps_in_yaml,
)
from skyvern.forge.sdk.copilot.workflow_block_traversal import (
    WorkflowBlockLocation,
    WorkflowBlockNodeLocation,
    workflow_block_locations,
    workflow_block_node_locations,
    workflow_link_node_mappings,
)
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
from skyvern.utils.yaml_loader import NoDatesSafeLoader, safe_load_no_dates

LOG = structlog.get_logger()

# Wide enough that safe_dump never folds a title onto a second line.
_YAML_NO_FOLD_WIDTH = 1 << 30


def runner_code_block_associations(
    workflow_yaml: str,
    *,
    prior_associations: dict[str, str] | None = None,
    preserve_existing: bool = False,
) -> dict[str, str]:
    """Assign opaque server-owned identities to code blocks without changing model-owned YAML.

    A server-composed block edit can explicitly retain a label's prior identity. A complete model
    replacement always receives fresh identities, even if it reproduces a prior label or source.
    """
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    associations: dict[str, str] = {}
    prior_associations = prior_associations or {}
    for location in workflow_block_locations(parsed):
        block = location.block
        label = block.get("label")
        if block.get("block_type") == "code" and isinstance(label, str) and label:
            associations[label] = (
                prior_associations[label] if preserve_existing and label in prior_associations else f"cba_{uuid4().hex}"
            )
    return associations


def dump_workflow_yaml(parsed: dict[str, Any]) -> str:
    """Serialize a parsed workflow without folding: a wrapped long line reloads as one joined
    string, which corrupts generated code."""
    return yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True, width=_YAML_NO_FOLD_WIDTH)


def reconcile_workflow_completion_contract(
    workflow_yaml: str,
    contract: dict[str, object] | None,
    *,
    previous_contract: dict[str, object] | None = None,
) -> tuple[str, bool]:
    """Reconcile an exact model-derived runtime contract and report an authoritative deletion.

    The contract comes from validated model-authored tool metadata. Parsing here interprets only
    machine-generated YAML structure; it does not classify user prose or generated code. Removing
    a declaration deletes only the exact prior model-derived contract, so an unrelated contract is
    never mistaken for metadata this seam owns. The boolean is the persistence-layer clear signal.
    """
    if contract is None and previous_contract is None:
        return workflow_yaml, False
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except Exception:
        return workflow_yaml, False
    if not isinstance(parsed, dict):
        return workflow_yaml, False
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return workflow_yaml, False
    if contract is None:
        if definition.get("completion_contract") != previous_contract:
            return workflow_yaml, False
        definition.pop("completion_contract", None)
        return dump_workflow_yaml(parsed), True
    if definition.get("completion_contract") == contract:
        return workflow_yaml, False
    definition["completion_contract"] = contract
    return dump_workflow_yaml(parsed), False


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


def _is_root_title_key(line: str) -> bool:
    """Whether ``line`` opens the document's own ``title`` key, quoted or not."""
    if not line or line[:1].isspace() or ":" not in line:
        return False
    return line.split(":", 1)[0].strip().strip("\"'") == "title"


def _verified_title_edit(original: str, edited: str, title: str) -> str:
    """Keep a line-level edit only when the parser agrees it set the root title.

    The scan reasons about lines while every consumer reasons about the parsed document;
    a shape that makes those disagree keeps the original rather than a broken draft.
    """
    try:
        if workflow_yaml_title(edited) == title:
            return edited
    except Exception:
        pass
    return original


def with_workflow_yaml_title(workflow_yaml: str | None, title: str) -> str:
    """Set ``title`` on a workflow YAML document, preserving the rest byte-for-byte.

    Line-level rather than a parse/dump round-trip, which would reflow the model's block
    scalars (code bodies, prompts) and churn every downstream diff.
    """
    # Sits on the proposal-persist path: a formatting concern must never cost the proposal.
    if not workflow_yaml or not isinstance(title, str) or not title.strip():
        return workflow_yaml or ""
    # Unbounded width: at PyYAML's default of 80 a long title folds onto a second line, and
    # a one-line replacement would leave the old continuation to be folded back into the new
    # scalar — which compounds on every accepted turn.
    serialized = yaml.safe_dump(
        {"title": title}, default_flow_style=False, allow_unicode=True, width=_YAML_NO_FOLD_WIDTH
    ).strip()
    lines = workflow_yaml.splitlines()
    trailing_newline = "\n" if workflow_yaml.endswith("\n") else ""
    for index, line in enumerate(lines):
        if not _is_root_title_key(line):
            continue
        # Drop the old value's continuation lines (a folded or block scalar), which would
        # otherwise dangle after the replacement and be absorbed into the new title. A blank
        # line inside a block scalar is part of it, so look past one for more indented text.
        end = index + 1
        while end < len(lines):
            if lines[end][:1].isspace() and lines[end].strip():
                end += 1
                continue
            following = next((offset for offset in range(end, len(lines)) if lines[offset].strip()), None)
            if not lines[end].strip() and following is not None and lines[following][:1].isspace():
                end = following + 1
                continue
            break
        lines[index:end] = [serialized]
        return _verified_title_edit(workflow_yaml, "\n".join(lines) + trailing_newline, title)
    # Prepending ahead of a leading ``---`` would make two documents, which safe_load rejects.
    insert_at = 1 if lines and lines[0].strip() == "---" else 0
    lines.insert(insert_at, serialized)
    return _verified_title_edit(workflow_yaml, "\n".join(lines) + trailing_newline, title)


def workflow_yaml_title(workflow_yaml: str | None) -> str | None:
    if not workflow_yaml:
        return None
    try:
        parsed = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    title = parsed.get("title")
    return title.strip() if isinstance(title, str) and title.strip() else None


def redact_credentials_in_workflow_yaml(
    workflow_yaml: str, workflow_permanent_id: str, credential_values: Collection[str]
) -> str:
    """The one document that gets persisted, anchored against, and shown to the model.

    A credential value should already have been rebound to a parameter reference upstream; reaching
    here with a live value means that failed, so redact rather than store it. The redacted workflow
    will not run as authored — that is the intended trade, and the error line is the signal to fix
    the upstream rebind.

    Apply this exactly once per document, at the write seam, before the draft is stored on the
    context and before it is persisted. A second application over its own output cannot be made
    faithful: a credential value that overlaps ``[REDACTED_SECRET]`` is indistinguishable from a
    marker the first pass wrote.

    ``credential_values`` must be scoped to the authoring session. Replacing values registered by
    other sessions would let one org's short secret rewrite another org's stored workflow.
    """
    from skyvern.forge.sdk.copilot.secret_scrub import MIN_PERSISTED_REDACTION_LENGTH, REDACTED_SECRET_PLACEHOLDER

    redactable = {
        value for value in credential_values if isinstance(value, str) and len(value) >= MIN_PERSISTED_REDACTION_LENGTH
    }
    # Redact to a marker no input can contain, then swap it for the placeholder at the end, so one
    # secret is never matched inside the placeholder another secret just produced.
    marker = "\x00"
    while marker in workflow_yaml or any(marker in secret for secret in redactable):
        marker += "\x00"
    redacted_count = 0
    # Longest first so an overlapping shorter value never splits a longer one.
    for secret in sorted(redactable, key=len, reverse=True):
        occurrences = workflow_yaml.count(secret)
        if occurrences:
            redacted_count += occurrences
            workflow_yaml = workflow_yaml.replace(secret, marker)
    workflow_yaml = workflow_yaml.replace(marker, REDACTED_SECRET_PLACEHOLDER)
    if redacted_count:
        LOG.error(
            "copilot redacted a raw credential value at the workflow write seam",
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
) -> Workflow:
    # Single seam every copilot YAML->Workflow conversion passes through, so code
    # blocks get their plain-view steps regardless of which path produced the YAML
    # (the update_workflow tool derives them upstream; the inline REPLACE_WORKFLOW
    # fallbacks would otherwise surface "No steps yet").
    workflow_yaml = derive_code_block_steps_in_yaml(workflow_yaml)
    # Same reasoning one field over: a block whose code names a declared parameter but omits it
    # from parameter_keys gets no value at runtime and dies on NameError mid-login.
    workflow_yaml = bind_referenced_parameters_in_yaml(workflow_yaml)
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

    # Omission-must-inherit, same as the settings above: the model re-emits whatever title it was
    # handed, so a draft still carrying the placeholder must not un-name an agent already named.
    title = workflow_yaml_request.title or ""
    if title in DEFAULT_WORKFLOW_TITLES or not title:
        # First candidate that is an actual name: the submitted draft may still carry the
        # placeholder while canonical holds a rename that landed mid-turn, and vice versa.
        candidates = (
            workflow_yaml_title(settings_fallback_yaml),
            settings_fallback_workflow.title if settings_fallback_workflow is not None else None,
        )
        for candidate in candidates:
            named = (candidate or "").strip()
            if named and named not in DEFAULT_WORKFLOW_TITLES:
                title = named
                break

    settings_source = settings_fallback_workflow or current_workflow
    if settings_source is None and (
        "cdp_connect_headers" not in workflow_yaml_request.model_fields_set
        or "max_elapsed_time_minutes" not in workflow_yaml_request.model_fields_set
    ):
        try:
            settings_source = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
        except WorkflowNotFound:
            settings_source = None
    cdp_connect_headers = workflow_yaml_request.cdp_connect_headers
    if "cdp_connect_headers" in workflow_yaml_request.model_fields_set:
        # An empty mapping preserves an explicit clear across later edits and reloads.
        cdp_connect_headers = cdp_connect_headers or {}
    elif settings_source is not None:
        cdp_connect_headers = settings_source.cdp_connect_headers
    max_elapsed_time_minutes = workflow_yaml_request.max_elapsed_time_minutes
    if "max_elapsed_time_minutes" not in workflow_yaml_request.model_fields_set and settings_source is not None:
        max_elapsed_time_minutes = settings_source.max_elapsed_time_minutes

    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id=workflow_id,
        organization_id=organization_id,
        title=title,
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
        reuse_browser_session=workflow_yaml_request.reuse_browser_session,
        mask_secrets=mask_secrets,
        pin_saved_session_ip=pin_saved_session_ip,
        browser_profile_id=workflow_yaml_request.browser_profile_id,
        browser_profile_key=workflow_yaml_request.browser_profile_key,
        model=workflow_yaml_request.model,
        max_screenshot_scrolls=workflow_yaml_request.max_screenshot_scrolls,
        max_elapsed_time_minutes=max_elapsed_time_minutes,
        generate_script_on_terminal=workflow_yaml_request.generate_script_on_terminal,
        status=workflow_yaml_request.status,
        extra_http_headers=workflow_yaml_request.extra_http_headers,
        cdp_connect_headers=cdp_connect_headers,
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


def _mapping_node_value(node: Node | None, key: str) -> tuple[ScalarNode, Node] | None:
    if not isinstance(node, MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == key:
            return key_node, value_node
    return None


def _compose_workflow_yaml(stored_yaml: str) -> Node | None:
    loader = NoDatesSafeLoader(stored_yaml)
    try:
        return loader.get_single_node()
    finally:
        loader.dispose()  # type: ignore[no-untyped-call]


def _node_location_by_label(root: Node | None, label: str) -> WorkflowBlockNodeLocation:
    locations = workflow_block_node_locations(root)
    matches = [location for location in locations if _node_label(location.block) == label]
    if not matches:
        raise BlockEditError(f"No block labelled {label!r}.")
    if len(matches) > 1:
        paths = ", ".join(location.path for location in matches)
        raise BlockEditError(
            f"{len(matches)} blocks share the label {label!r}; labels must be unique to edit one. Paths: {paths}."
        )
    return matches[0]


def _node_label(block: MappingNode) -> str | None:
    label_pair = _mapping_node_value(block, "label")
    label_node = label_pair[1] if label_pair else None
    return label_node.value if isinstance(label_node, ScalarNode) else None


def _code_scalar_source(stored_yaml: str, label: str) -> tuple[Node, ScalarNode, int]:
    """Locate one existing block's code using PyYAML's parser-owned source marks."""
    root = _compose_workflow_yaml(stored_yaml)
    if root is None or not workflow_block_node_locations(root):
        raise BlockEditError("The stored workflow has no workflow_definition.blocks to edit.")
    location = _node_location_by_label(root, label)
    _require_unaliased_value(root, location.block)
    code_pair = _mapping_node_value(location.block, "code")
    if code_pair is None or not isinstance(code_pair[1], ScalarNode):
        raise BlockEditError(f"Block {label!r} has no code to edit.")
    return root, code_pair[1], int(code_pair[0].start_mark.column) + 2


def _node_reference_count(root: Node, target: Node) -> int:
    counts: dict[int, int] = {id(root): 1}
    stack = [root]
    expanded: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in expanded:
            continue
        expanded.add(id(node))
        children = (
            [child for pair in node.value for child in pair]
            if isinstance(node, MappingNode)
            else list(node.value)
            if isinstance(node, SequenceNode)
            else []
        )
        for child in children:
            counts[id(child)] = counts.get(id(child), 0) + 1
            stack.append(child)
    return counts.get(id(target), 0)


def _require_unaliased_value(root: Node, value_node: Node) -> None:
    if _node_reference_count(root, value_node) > 1:
        raise BlockEditError("This field is used by a YAML alias and cannot be edited without changing another field.")


def _render_code_scalar(code: str, *, content_indent: int, source_style: str | None) -> str:
    """Serialize model-authored code as one YAML scalar without touching surrounding bytes."""
    if not code:
        return "''\n"
    if source_style not in {"|", ">"}:
        dumped = yaml.safe_dump(code, allow_unicode=True, default_flow_style=True, width=_YAML_NO_FOLD_WIDTH)
        if dumped.endswith("\n...\n"):
            return dumped[: -len("\n...\n")]
        return dumped.removesuffix("\n")

    trailing_newlines = len(code) - len(code.rstrip("\n"))
    chomp = "+" if trailing_newlines > 1 else "" if trailing_newlines == 1 else "-"
    first_nonempty = next((line for line in code.splitlines() if line), "")
    indentation_indicator = "2" if first_nonempty[:1].isspace() else ""
    indicator = f"{source_style}{indentation_indicator}{chomp}"
    indentation = " " * content_indent
    body = "".join(f"{indentation}{line}" for line in code.splitlines(keepends=True))
    if not code.endswith("\n"):
        body += "\n"
    return f"{indicator}\n{body}"


def _replace_code_scalar_source(stored_yaml: str, label: str, code: str) -> str:
    root, scalar, content_indent = _code_scalar_source(stored_yaml, label)
    _require_unaliased_value(root, scalar)
    replacement = _render_code_scalar_replacement(stored_yaml, scalar, code, content_indent)
    return stored_yaml[: scalar.start_mark.index] + replacement + stored_yaml[scalar.end_mark.index :]


def _render_code_scalar_replacement(stored_yaml: str, scalar: ScalarNode, code: str, content_indent: int) -> str:
    source_style = "|" if scalar.style == ">" and "\n" in code.rstrip("\n") else scalar.style
    replacement = _render_code_scalar(code, content_indent=content_indent, source_style=source_style)
    header_preserved = False
    if scalar.style in {"|", ">"} and source_style == scalar.style and code:
        current_trailing_newlines = len(scalar.value) - len(scalar.value.rstrip("\n"))
        replacement_trailing_newlines = len(code) - len(code.rstrip("\n"))
        current_first_nonempty = next((line for line in scalar.value.splitlines() if line), "")
        replacement_first_nonempty = next((line for line in code.splitlines() if line), "")
        indentation_shape_unchanged = current_first_nonempty[:1].isspace() == replacement_first_nonempty[:1].isspace()
        source = stored_yaml[scalar.start_mark.index : scalar.end_mark.index]
        source_header_end = source.find("\n")
        replacement_header_end = replacement.find("\n")
        if (
            current_trailing_newlines == replacement_trailing_newlines
            and indentation_shape_unchanged
            and min(source_header_end, replacement_header_end) >= 0
        ):
            replacement = source[: source_header_end + 1] + replacement[replacement_header_end + 1 :]
            header_preserved = True
    if scalar.style in {"|", ">"} and not header_preserved:
        source = stored_yaml[scalar.start_mark.index : scalar.end_mark.index]
        source_header_end = source.find("\n")
        replacement_header_end = replacement.find("\n")
        if min(source_header_end, replacement_header_end) >= 0:
            source_header = source[:source_header_end]
            replacement_header = replacement[:replacement_header_end]
            style_index = source_header.find(scalar.style)
            comment_index = source_header.find("#", style_index + 1)
            metadata_prefix = source_header[:style_index] if style_index >= 0 else ""
            comment_suffix = source_header[comment_index - 1 :] if comment_index > 0 else ""
            replacement = metadata_prefix + replacement_header + comment_suffix + replacement[replacement_header_end:]
    return replacement


def _top_level_workflow_blocks(parsed: Any) -> list[Any] | None:
    if not isinstance(parsed, dict):
        return None
    definition = parsed.get("workflow_definition")
    if not isinstance(definition, dict):
        return None
    blocks = definition.get("blocks")
    return blocks if isinstance(blocks, list) else None


def _block_by_label(parsed: dict[str, Any], label: str) -> WorkflowBlockLocation:
    locations = workflow_block_locations(parsed)
    matches = [location for location in locations if str(location.block.get("label") or "") == label]
    if not matches:
        known = ", ".join(sorted(str(location.block.get("label") or "") for location in locations))
        raise BlockEditError(f"No block labelled {label!r}. The workflow has: {known or '(no labelled blocks)'}.")
    if len(matches) > 1:
        paths = ", ".join(location.path for location in matches)
        raise BlockEditError(
            f"{len(matches)} blocks share the label {label!r}; labels must be unique to edit one. Paths: {paths}."
        )
    return matches[0]


def _top_level_block_by_label(blocks: list[Any], label: str) -> dict[str, Any]:
    matches = [block for block in blocks if isinstance(block, dict) and str(block.get("label") or "") == label]
    if not matches:
        known = ", ".join(sorted(str(block.get("label") or "") for block in blocks if isinstance(block, dict)))
        raise BlockEditError(
            f"No top-level block labelled {label!r}. The workflow has these top-level blocks: "
            f"{known or '(no labelled blocks)'}."
        )
    if len(matches) > 1:
        raise BlockEditError(f"{len(matches)} top-level blocks share the label {label!r}; labels must be unique.")
    return matches[0]


def _render_flow_yaml_value(value: Any) -> str:
    rendered = yaml.safe_dump(
        value,
        allow_unicode=True,
        default_flow_style=True,
        sort_keys=False,
        width=_YAML_NO_FOLD_WIDTH,
    )
    return rendered.removesuffix("\n...\n").removesuffix("\n")


def _apply_source_edits(stored_yaml: str, edits: list[tuple[int, int, str]]) -> str:
    result = stored_yaml
    previous_start = len(stored_yaml) + 1
    for start, end, replacement in sorted(edits, reverse=True):
        if start < 0 or end < start or end > len(stored_yaml) or end > previous_start:
            raise BlockEditError("The requested block mutations overlap in the stored YAML.")
        result = result[:start] + replacement + result[end:]
        previous_start = start
    return result


def _mapping_append_index(stored_yaml: str, block_node: MappingNode) -> int:
    if not block_node.value:
        return block_node.end_mark.index
    block_end = block_node.end_mark.index
    lower_bound = block_end if block_end == len(stored_yaml) else stored_yaml.rfind("\n", 0, block_end) + 1
    return max(lower_bound, _node_content_line_end(stored_yaml, block_node.value[-1][1]))


def _render_inserted_fields(fields: dict[str, Any], indent: int) -> str:
    rendered = yaml.safe_dump(fields, allow_unicode=True, sort_keys=False, width=_YAML_NO_FOLD_WIDTH)
    indentation = " " * indent
    return "".join(f"{indentation}{line}" for line in rendered.splitlines(keepends=True))


def _render_node_replacement(stored_yaml: str, value_node: Node, value: Any, minimum_indent: int) -> str:
    replacement = " " * max(0, minimum_indent - value_node.start_mark.column) + _render_flow_yaml_value(value)
    is_multiline_block_value = isinstance(value_node, (MappingNode, SequenceNode)) or (
        isinstance(value_node, ScalarNode) and value_node.style in {"|", ">"}
    )
    if not is_multiline_block_value or value_node.end_mark.line == value_node.start_mark.line:
        return replacement
    if value_node.end_mark.column:
        return f"{replacement}\n{' ' * value_node.end_mark.column}"
    source = stored_yaml[value_node.start_mark.index : value_node.end_mark.index]
    return replacement + ("\n" if source[-1:] == "\n" else "")


def _replace_block_fields_source(stored_yaml: str, label: str, fields: dict[str, Any]) -> str:
    root = _compose_workflow_yaml(stored_yaml)
    if root is None:
        raise BlockEditError("The stored workflow has no workflow_definition.blocks to edit.")
    location = _node_location_by_label(root, label)
    _require_unaliased_value(root, location.block)
    edits: list[tuple[int, int, str]] = []
    inserted: dict[str, Any] = {}
    for key, value in fields.items():
        pair = _mapping_node_value(location.block, key)
        if pair is None:
            inserted[key] = value
            continue
        key_node, value_node = pair
        _require_unaliased_value(root, value_node)
        replacement = (
            _render_code_scalar_replacement(stored_yaml, value_node, value, key_node.start_mark.column + 2)
            if key == "code" and isinstance(value, str) and isinstance(value_node, ScalarNode)
            else _render_node_replacement(stored_yaml, value_node, value, key_node.start_mark.column + 2)
        )
        edits.append((value_node.start_mark.index, value_node.end_mark.index, replacement))

    if inserted:
        if location.block.flow_style:
            suffix = ", " if location.block.value else ""
            rendered = _render_flow_yaml_value(inserted)
            edits.append(
                (location.block.end_mark.index - 1, location.block.end_mark.index - 1, suffix + rendered[1:-1])
            )
        else:
            indent = location.block.value[0][0].start_mark.column
            insertion = _mapping_append_index(stored_yaml, location.block)
            prefix = "" if insertion == 0 or stored_yaml[insertion - 1 : insertion] == "\n" else "\n"
            edits.append((insertion, insertion, prefix + _render_inserted_fields(inserted, indent)))
    return _apply_source_edits(stored_yaml, edits)


def _sequence_item_source_span(
    stored_yaml: str, location: WorkflowBlockNodeLocation, value_indent: int
) -> tuple[int, int, str]:
    owner = location.owner
    if owner.flow_style:
        if len(owner.value) == 1:
            return owner.start_mark.index, owner.end_mark.index, "[]"
        if location.index < len(owner.value) - 1:
            return location.block.start_mark.index, owner.value[location.index + 1].start_mark.index, ""
        previous = owner.value[location.index - 1]
        return previous.end_mark.index, location.block.end_mark.index, ""
    if len(owner.value) == 1:
        end = _node_content_line_end(stored_yaml, location.block)
        trailing_newline = "\n" if end > owner.start_mark.index and stored_yaml[end - 1 : end] == "\n" else ""
        return owner.start_mark.index, end, f"{' ' * (value_indent - owner.start_mark.column)}[]{trailing_newline}"
    start = stored_yaml.rfind("\n", 0, location.block.start_mark.index) + 1
    end = _node_content_line_end(stored_yaml, location.block)
    return start, end, ""


def _node_content_line_end(stored_yaml: str, node: Node) -> int:
    if isinstance(node, MappingNode) and node.value:
        return _node_content_line_end(stored_yaml, node.value[-1][1])
    if isinstance(node, SequenceNode) and node.value:
        return _node_content_line_end(stored_yaml, node.value[-1])
    end = node.end_mark.index
    if isinstance(node, ScalarNode) and node.style in {"|", ">"} and node.end_mark.column:
        end = stored_yaml.rfind("\n", 0, end) + 1
    if end > 0 and stored_yaml[end - 1 : end] == "\n":
        return end
    newline = stored_yaml.find("\n", end)
    return newline + 1 if newline >= 0 else len(stored_yaml)


def _unique_nodes(root: Node) -> list[Node]:
    nodes: list[Node] = []
    stack = [root]
    visited: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in visited:
            continue
        visited.add(id(node))
        nodes.append(node)
        if isinstance(node, MappingNode):
            stack.extend(child for pair in node.value for child in pair)
        elif isinstance(node, SequenceNode):
            stack.extend(node.value)
    return nodes


def _sequence_value_indent(root: Node, sequence: SequenceNode) -> int:
    nodes = _unique_nodes(root)
    for node in nodes:
        if not isinstance(node, MappingNode):
            continue
        for key_node, value_node in node.value:
            if value_node is sequence:
                return max(int(sequence.start_mark.column), int(key_node.start_mark.column) + 2)
    return int(sequence.start_mark.column)


def stored_workflow_yaml(copilot_ctx: Any) -> str:
    """The workflow a block-scoped edit is applied to: the last accepted write, else the turn's draft.

    Every surface that shows the model a block's code must read it from here, or the code it reads
    and the code its edit is anchored against can be two different things.
    """
    latest = getattr(copilot_ctx, "last_workflow_yaml", None)
    if isinstance(latest, str) and latest.strip():
        return latest
    stored = getattr(copilot_ctx, "workflow_yaml", None)
    return stored if isinstance(stored, str) else ""


def stored_block_code(stored_yaml: str, label: str) -> str | None:
    """The code ``apply_block_edit`` would anchor an edit to ``label`` against, if any."""
    if not label or not stored_yaml.strip():
        return None
    try:
        parsed = safe_load_no_dates(stored_yaml)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        block = _block_by_label(parsed, label).block
    except BlockEditError:
        return None
    code = block.get("code")
    return code if isinstance(code, str) and code.strip() else None


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
    if not isinstance(parsed, dict) or _top_level_workflow_blocks(parsed) is None:
        raise BlockEditError("The stored workflow has no workflow_definition.blocks to edit.")
    block = _block_by_label(parsed, label).block

    edited_code: str | None = None
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
        edited_code = current.replace(expected_code, replacement_code, 1)

    if edited_code is not None and not fields:
        return _replace_code_scalar_source(stored_yaml, label, edited_code)
    mutations = dict(fields or {})
    if edited_code is not None and "code" not in mutations:
        mutations["code"] = edited_code
    return _replace_block_fields_source(stored_yaml, label, mutations)


def _merge_new_workflow_parameters(parsed: dict[str, Any], parameters: list[Any]) -> None:
    definition = parsed["workflow_definition"]
    existing = definition.get("parameters")
    if not isinstance(existing, list):
        existing = []
    declared = {str(p.get("key") or "") for p in existing if isinstance(p, dict)}
    for parameter in parameters:
        if not isinstance(parameter, dict):
            raise BlockEditError("Each entry in parameters must be a mapping with a key.")
        key = str(parameter.get("key") or "")
        if not key:
            raise BlockEditError("Each entry in parameters needs a key.")
        # Declaring is additive only: a key the workflow already has keeps its current definition.
        if key in declared:
            continue
        existing.append(parameter)
        declared.add(key)
    definition["parameters"] = existing


def add_block_to_workflow(
    stored_yaml: str,
    after_label: str,
    block_yaml: str,
    *,
    parameters: list[Any] | None = None,
) -> str:
    """Splice one new block in after ``after_label`` and return the whole workflow.

    The predecessor hands its ``next_block_label`` to the new block and points at it instead, so the
    chain stays intact without the model retyping the blocks it is not changing.
    """
    try:
        parsed = safe_load_no_dates(stored_yaml)
    except Exception as exc:
        raise BlockEditError(f"The stored workflow is not parseable: {exc}") from exc
    blocks = _top_level_workflow_blocks(parsed)
    if blocks is None:
        raise BlockEditError("The stored workflow has no workflow_definition.blocks to edit.")
    predecessor = _top_level_block_by_label(blocks, after_label)

    try:
        new_block = safe_load_no_dates(block_yaml)
    except Exception as exc:
        raise BlockEditError(f"block_yaml is not parseable: {exc}") from exc
    if not isinstance(new_block, dict):
        raise BlockEditError("block_yaml must be a single block mapping.")
    new_label = str(new_block.get("label") or "")
    if not new_label:
        raise BlockEditError("The new block needs a label.")
    known = sorted(str(location.block.get("label") or "") for location in workflow_block_locations(parsed))
    if new_label in known:
        raise BlockEditError(
            f"A block labelled {new_label!r} already exists. The workflow has: {', '.join(known)}. "
            "Use edit_block to change it, or give the new block a different label."
        )

    new_block["next_block_label"] = predecessor.get("next_block_label")
    predecessor["next_block_label"] = new_label
    blocks.insert(blocks.index(predecessor) + 1, new_block)

    if parameters:
        _merge_new_workflow_parameters(parsed, parameters)
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
    if not isinstance(parsed, dict) or _top_level_workflow_blocks(parsed) is None:
        raise BlockEditError("The stored workflow has no workflow_definition.blocks to edit.")
    _block_by_label(parsed, label)

    root = _compose_workflow_yaml(stored_yaml)
    if root is None:
        raise BlockEditError("The stored workflow has no workflow_definition.blocks to edit.")
    target = _node_location_by_label(root, label)
    _require_unaliased_value(root, target.block)
    _require_unaliased_value(root, target.owner)
    target_start, target_end, replacement = _sequence_item_source_span(
        stored_yaml, target, _sequence_value_indent(root, target.owner)
    )
    for node in _unique_nodes(root):
        if target_start <= node.start_mark.index and node.end_mark.index <= target_end:
            _require_unaliased_value(root, node)
    edits = [(target_start, target_end, replacement)]
    seen_pointer_spans: set[tuple[int, int]] = set()
    for mapping in workflow_link_node_mappings(root):
        pointer_pair = _mapping_node_value(mapping, "next_block_label")
        pointer = pointer_pair[1] if pointer_pair else None
        if not isinstance(pointer, ScalarNode) or pointer.value != label:
            continue
        if target_start <= pointer.start_mark.index and pointer.end_mark.index <= target_end:
            continue
        span = (pointer.start_mark.index, pointer.end_mark.index)
        if span in seen_pointer_spans:
            continue
        _require_unaliased_value(root, mapping)
        _require_unaliased_value(root, pointer)
        seen_pointer_spans.add(span)
        edits.append((pointer.start_mark.index, pointer.end_mark.index, "null"))
    return _apply_source_edits(stored_yaml, edits)
