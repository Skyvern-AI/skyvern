"""Parity guard between the backend BlockType enum and the SDK client's block unions.

The SDK client at ``skyvern/client/`` is maintained by hand. When a new block
type is added to ``skyvern/schemas/workflows.BlockType`` without adding it to
the client, read paths that deserialize through the client's ``Workflow`` type
break (see ``test_mcp_workflow_list_drift``). The MCP tools bypass the client
for Workflow reads (see SKY-9227 and the Skyvern Cloud-only SDK client
runbook), but callers that use the SDK directly still need the block.

This test introspects both discriminated unions and diffs them against the
backend enum. Known-drifted values are tolerated via an explicit allowlist
with per-entry ticket / owner / added_at metadata; any NEW drift fails CI with
a checklist that points to the runbook.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from datetime import date

import pytest

from skyvern.client.types.workflow_definition_blocks_item import WorkflowDefinitionBlocksItem
from skyvern.client.types.workflow_definition_yaml_blocks_item import WorkflowDefinitionYamlBlocksItem
from skyvern.schemas.workflows import BlockType

_DOC_POINTER = "the Skyvern Cloud-only SDK client runbook (not part of OSS sync)"

# The failure message for drift checks IS the new-block-type checklist. That
# way the guidance only surfaces to devs whose work actually triggers it — no
# noise on unrelated PRs, impossible to miss on block-type PRs.
_NEW_BLOCK_TYPE_CHECKLIST = (
    "A new BlockType addition must satisfy ALL of the following before merge:\n"
    "  1. Added to `skyvern/schemas/workflows.BlockType` enum.\n"
    "  2. YAML class added to `skyvern/schemas/workflows.BLOCK_YAML_SUBCLASSES` (BLOCK_TYPE_MAP derives from it)\n"
    "     and a `skyvern/cli/mcp_tools/blocks.BLOCK_SUMMARIES` entry added.\n"
    "  3. EITHER add the block to the SDK clients in this PR (`skyvern/client/types/`\n"
    "     and `skyvern-ts/client/src/api/types/`),\n"
    "     OR add a `BlockDriftEntry` to `_KNOWN_DRIFT_ALLOWLIST` in this file with\n"
    "     populated `ticket`, `owner`, and `added_at` fields.\n"
    "  4. Tested with MCP `skyvern_workflow_list` / `_create` / `_update` against a\n"
    "     workflow that contains the new block type.\n"
    "  5. If the block introduces new top-level fields, verify that\n"
    "     `skyvern.cli.mcp_tools.workflow._normalize_json_definition` round-trips\n"
    "     them (unrecognized fields silently drop if the internal schema is stricter\n"
    "     than the API boundary).\n"
    f"\nSee {_DOC_POINTER} for how the SDK clients are maintained, the allowlist policy,"
    "\nand the decision record on why unknown-block compat lives in MCP, not the SDK."
)


@dataclass(frozen=True)
class BlockDriftEntry:
    """An allowlisted backend BlockType that the SDK client does not yet know about.

    Every entry must carry a live Linear ticket tracking the SDK work and a
    named owner accountable for clearing the entry once the client has the block.
    ``added_at`` is informational only — there is deliberately no time-based
    staleness failure; entries are cleared when the client catches up, not by calendar.

    Construction-time ``__post_init__`` enforces non-empty ticket / owner /
    block_type — a stub-pasted entry fails at module import, not at test time.

    See the Skyvern Cloud-only SDK client runbook for the allowlist policy.
    """

    block_type: str
    ticket: str
    owner: str
    added_at: date
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.block_type:
            raise ValueError("BlockDriftEntry.block_type must be a non-empty backend enum value.")
        if not self.ticket:
            raise ValueError(
                f"BlockDriftEntry({self.block_type!r}).ticket must reference a Linear ticket (e.g. 'SKY-1234'). "
                f"See {_DOC_POINTER} for the allowlist policy."
            )
        if not self.owner:
            raise ValueError(
                f"BlockDriftEntry({self.block_type!r}).owner must name an accountable GitHub handle "
                f"(e.g. '@wintonzheng'). See {_DOC_POINTER}."
            )
        if not isinstance(self.added_at, date):
            raise TypeError(
                f"BlockDriftEntry({self.block_type!r}).added_at must be a datetime.date, "
                f"got {type(self.added_at).__name__}."
            )


# Allowlist: block types present in the backend but not yet in the SDK client.
# Add the block to the client, then remove the entry here.
_KNOWN_DRIFT_ALLOWLIST: tuple[BlockDriftEntry, ...] = (
    BlockDriftEntry(
        block_type="web_search",
        ticket="SKY-16450",
        owner="@suchintan",
        added_at=date(2026, 9, 16),
        notes="Web search is not in the SDK client yet.",
    ),
    BlockDriftEntry(
        block_type="data_export",
        ticket="SKY-14102",
        owner="@wintonzheng",
        added_at=date(2026, 8, 17),
        notes="Parquet data export is not in the SDK client yet.",
    ),
    BlockDriftEntry(
        block_type="terminate",
        ticket="SKY-16910",
        owner="@nickjtch-skyvern",
        added_at=date(2026, 9, 23),
        notes="Terminate block is not in the SDK client yet.",
    ),
    BlockDriftEntry(
        block_type="agent_step",
        ticket="SKY-19057",
        owner="@andrewneilson",
        added_at=date(2026, 10, 26),
        notes="Agent block prototype is not in the SDK client yet.",
    ),
    BlockDriftEntry(
        block_type="powerbi_query",
        ticket="SKY-18242",
        owner="@AronPerez",
        added_at=date(2026, 10, 29),
        notes="Offline Power BI block is accepted by raw-HTTP MCP; the typed SDK client does not have it yet.",
    ),
    BlockDriftEntry(
        block_type="integration_action",
        ticket="SKY-20689",
        owner="@cindehaa",
        added_at=date(2026, 11, 16),
        notes="Generic integration action block is not in the SDK client yet.",
    ),
)


def _allowlisted_block_types() -> set[str]:
    return {entry.block_type for entry in _KNOWN_DRIFT_ALLOWLIST}


def _union_block_types(union_type: typing.Any) -> set[str]:
    """Extract the ``block_type`` Literal value from every variant of a client union."""
    values: set[str] = set()
    for variant in typing.get_args(union_type):
        annotation = variant.model_fields["block_type"].annotation
        literal_args = typing.get_args(annotation)
        if not literal_args:
            pytest.fail(f"Client variant {variant.__name__} has a non-Literal block_type annotation: {annotation!r}")
        values.update(str(arg) for arg in literal_args)
    return values


def _currently_drifted_block_types(
    backend_values: set[str],
    client_blocks: set[str],
    client_yaml_blocks: set[str],
) -> set[str]:
    missing_in_client = backend_values - (client_blocks & client_yaml_blocks)
    extra_in_client = (client_blocks | client_yaml_blocks) - backend_values
    union_mismatch = client_blocks ^ client_yaml_blocks
    return missing_in_client | extra_in_client | union_mismatch


def test_backend_block_types_present_in_client_unions() -> None:
    """Every BlockType value must be known to both client unions (or allowlisted)."""
    backend_values = {member.value for member in BlockType}

    client_blocks = _union_block_types(WorkflowDefinitionBlocksItem)
    client_yaml_blocks = _union_block_types(WorkflowDefinitionYamlBlocksItem)
    client_known = client_blocks & client_yaml_blocks

    missing_in_client = backend_values - client_known - _allowlisted_block_types()
    assert not missing_in_client, (
        f"SDK client drift detected: backend `skyvern.schemas.workflows.BlockType` has "
        f"value(s) {sorted(missing_in_client)!r} that the client's discriminated unions "
        f"(`WorkflowDefinitionBlocksItem` / `WorkflowDefinitionYamlBlocksItem`) do not know.\n\n"
        f"{_NEW_BLOCK_TYPE_CHECKLIST}"
    )


def test_client_union_variants_are_subset_of_backend_or_deprecated() -> None:
    """The client may legitimately retain retired types during a deprecation window.

    This is intentionally a hard failure unless the drift is allowlisted;
    stale entries are then caught by the allowlist cleanup test.
    """
    backend_values = {member.value for member in BlockType}

    client_blocks = _union_block_types(WorkflowDefinitionBlocksItem)
    client_yaml_blocks = _union_block_types(WorkflowDefinitionYamlBlocksItem)

    extras = (client_blocks | client_yaml_blocks) - backend_values
    unexpected = extras - _allowlisted_block_types()
    assert not unexpected, (
        f"The client's unions reference block_type value(s) {sorted(unexpected)!r} that are "
        f"not in the backend BlockType enum. If this is a planned deprecation, add a "
        f"`BlockDriftEntry` to `_KNOWN_DRIFT_ALLOWLIST` with ticket/owner/added_at; "
        f"otherwise investigate.\n\n"
        f"{_NEW_BLOCK_TYPE_CHECKLIST}"
    )


def test_allowlist_entries_are_actually_drifted() -> None:
    """Keeps _KNOWN_DRIFT_ALLOWLIST honest: drop an entry once the client has the block.

    Once an entry is no longer drifted because the block was added to the
    client, delete it from the allowlist.
    """
    backend_values = {member.value for member in BlockType}

    client_blocks = _union_block_types(WorkflowDefinitionBlocksItem)
    client_yaml_blocks = _union_block_types(WorkflowDefinitionYamlBlocksItem)

    currently_drifted = _currently_drifted_block_types(backend_values, client_blocks, client_yaml_blocks)
    stale = _allowlisted_block_types() - currently_drifted
    assert not stale, (
        f"_KNOWN_DRIFT_ALLOWLIST contains stale entries {sorted(stale)!r} that no "
        f"longer drift. Remove them to keep the allowlist tight. "
        f"See {_DOC_POINTER}."
    )
