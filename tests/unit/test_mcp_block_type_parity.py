"""Parity guard between the backend BlockType enum and the Fern SDK unions.

The vendored Fern SDK at ``skyvern/client/`` is regenerated manually. When a
new block type is added to ``skyvern/schemas/workflows.BlockType`` without
regenerating the SDK, MCP read paths that still deserialize through the Fern
``Workflow`` type break (see ``test_mcp_workflow_list_drift``). Even though the
MCP tools now bypass Fern for Workflow reads (see SKY-9227 and
the Skyvern Cloud-only ``cloud_docs/fern-sdk/README.md`` runbook), we still
want an early warning so downstream clients that use the Fern SDK directly stay
in sync.

This test introspects both Fern discriminated unions and diffs them against
the backend enum. Known-drifted values are tolerated via an explicit allowlist
with per-entry ticket / owner / added_at metadata; any NEW drift fails CI with
a checklist that points to the regen doc.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from datetime import date

import pytest

from skyvern.client.types.workflow_definition_blocks_item import WorkflowDefinitionBlocksItem
from skyvern.client.types.workflow_definition_yaml_blocks_item import WorkflowDefinitionYamlBlocksItem
from skyvern.schemas.workflows import BlockType

_DOC_POINTER = "Skyvern Cloud-only runbook: cloud_docs/fern-sdk/README.md (not part of OSS sync)"

# The failure message for drift checks IS the new-block-type checklist. That
# way the guidance only surfaces to devs whose work actually triggers it — no
# noise on unrelated PRs, impossible to miss on block-type PRs.
_NEW_BLOCK_TYPE_CHECKLIST = (
    "A new BlockType addition must satisfy ALL of the following before merge:\n"
    "  1. Added to `skyvern/schemas/workflows.BlockType` enum.\n"
    "  2. Added to `skyvern/cli/mcp_tools/blocks.BLOCK_TYPE_MAP` AND `_YAML_CLASS_MAP`.\n"
    "  3. EITHER regenerate the Fern SDK in a paired PR (link it),\n"
    "     OR add a `BlockDriftEntry` to `_KNOWN_DRIFT_ALLOWLIST` in this file with\n"
    "     populated `ticket`, `owner`, and `added_at` fields.\n"
    "  4. Tested with MCP `skyvern_workflow_list` / `_create` / `_update` against a\n"
    "     workflow that contains the new block type.\n"
    "  5. If the block introduces new top-level fields, verify that\n"
    "     `skyvern.cli.mcp_tools.workflow._normalize_json_definition` round-trips\n"
    "     them (unrecognized fields silently drop if the internal schema is stricter\n"
    "     than the API boundary).\n"
    f"\nSee {_DOC_POINTER} for the full regeneration process, allowlist policy, and"
    "\nthe decision record on why unknown-block compat lives in MCP, not Fern."
)


@dataclass(frozen=True)
class BlockDriftEntry:
    """An allowlisted backend BlockType that the Fern SDK does not yet know about.

    Every entry must carry a live Linear ticket tracking the regen work and a
    named owner accountable for clearing the entry at the next Fern regeneration.
    ``added_at`` is informational only — there is deliberately no time-based
    staleness failure; entries are cleared at regen time, not by calendar.

    Construction-time ``__post_init__`` enforces non-empty ticket / owner /
    block_type — a stub-pasted entry fails at module import, not at test time.

    See the Skyvern Cloud-only Fern runbook for the allowlist policy.
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
                f"(e.g. '@marc'). See {_DOC_POINTER}."
            )
        if not isinstance(self.added_at, date):
            raise TypeError(
                f"BlockDriftEntry({self.block_type!r}).added_at must be a datetime.date, "
                f"got {type(self.added_at).__name__}."
            )


# Allowlist: block types present in the backend but not yet regenerated into
# the vendored Fern SDK. Run `fern generate` using the Skyvern Cloud-only
# runbook to resync, then remove the entry here.
_KNOWN_DRIFT_ALLOWLIST: tuple[BlockDriftEntry, ...] = (
    BlockDriftEntry(
        block_type="google_sheets_read",
        ticket="SKY-9227",
        owner="@marc",
        added_at=date(2026, 4, 23),
        notes="Clears with `google_sheets_write` at the next Fern SDK regeneration.",
    ),
    BlockDriftEntry(
        block_type="google_sheets_write",
        ticket="SKY-9227",
        owner="@marc",
        added_at=date(2026, 4, 23),
        notes="Clears with `google_sheets_read` at the next Fern SDK regeneration.",
    ),
)


def _allowlisted_block_types() -> set[str]:
    return {entry.block_type for entry in _KNOWN_DRIFT_ALLOWLIST}


def _fern_union_block_types(union_type: typing.Any) -> set[str]:
    """Extract the ``block_type`` Literal value from every variant of a Fern Union."""
    values: set[str] = set()
    for variant in typing.get_args(union_type):
        annotation = variant.model_fields["block_type"].annotation
        literal_args = typing.get_args(annotation)
        if not literal_args:
            pytest.fail(f"Fern variant {variant.__name__} has a non-Literal block_type annotation: {annotation!r}")
        values.update(str(arg) for arg in literal_args)
    return values


def _currently_drifted_block_types(
    backend_values: set[str],
    fern_blocks: set[str],
    fern_yaml_blocks: set[str],
) -> set[str]:
    missing_in_fern = backend_values - (fern_blocks & fern_yaml_blocks)
    extra_in_fern = (fern_blocks | fern_yaml_blocks) - backend_values
    fern_union_mismatch = fern_blocks ^ fern_yaml_blocks
    return missing_in_fern | extra_in_fern | fern_union_mismatch


def test_backend_block_types_present_in_fern_unions() -> None:
    """Every BlockType value must be known to both Fern unions (or allowlisted)."""
    backend_values = {member.value for member in BlockType}

    fern_blocks = _fern_union_block_types(WorkflowDefinitionBlocksItem)
    fern_yaml_blocks = _fern_union_block_types(WorkflowDefinitionYamlBlocksItem)
    fern_known = fern_blocks & fern_yaml_blocks

    missing_in_fern = backend_values - fern_known - _allowlisted_block_types()
    assert not missing_in_fern, (
        f"Fern SDK drift detected: backend `skyvern.schemas.workflows.BlockType` has "
        f"value(s) {sorted(missing_in_fern)!r} that the Fern discriminated unions "
        f"(`WorkflowDefinitionBlocksItem` / `WorkflowDefinitionYamlBlocksItem`) do not know.\n\n"
        f"{_NEW_BLOCK_TYPE_CHECKLIST}"
    )


def test_fern_union_variants_are_subset_of_backend_or_deprecated() -> None:
    """Fern may legitimately retain retired types during a deprecation window.

    This is intentionally a hard failure unless the drift is allowlisted for
    one regen cycle; stale entries are then removed by the regen cleanup test.
    """
    backend_values = {member.value for member in BlockType}

    fern_blocks = _fern_union_block_types(WorkflowDefinitionBlocksItem)
    fern_yaml_blocks = _fern_union_block_types(WorkflowDefinitionYamlBlocksItem)

    extras = (fern_blocks | fern_yaml_blocks) - backend_values
    unexpected = extras - _allowlisted_block_types()
    assert not unexpected, (
        f"Fern unions reference block_type value(s) {sorted(unexpected)!r} that are "
        f"not in the backend BlockType enum. If this is a planned deprecation, add a "
        f"`BlockDriftEntry` to `_KNOWN_DRIFT_ALLOWLIST` with ticket/owner/added_at; "
        f"otherwise investigate.\n\n"
        f"{_NEW_BLOCK_TYPE_CHECKLIST}"
    )


def test_allowlist_entries_are_actually_drifted() -> None:
    """Keeps _KNOWN_DRIFT_ALLOWLIST honest: drop stale entries after Fern regen.

    This is the regen-time cleanliness gate (see the Skyvern Cloud-only Fern
    runbook post-regeneration steps). Once an entry is no longer drifted because a regen
    landed, delete it from the allowlist.
    """
    backend_values = {member.value for member in BlockType}

    fern_blocks = _fern_union_block_types(WorkflowDefinitionBlocksItem)
    fern_yaml_blocks = _fern_union_block_types(WorkflowDefinitionYamlBlocksItem)

    currently_drifted = _currently_drifted_block_types(backend_values, fern_blocks, fern_yaml_blocks)
    stale = _allowlisted_block_types() - currently_drifted
    assert not stale, (
        f"_KNOWN_DRIFT_ALLOWLIST contains stale entries {sorted(stale)!r} that no "
        f"longer drift. Remove them to keep the allowlist tight. "
        f"See {_DOC_POINTER} post-regeneration steps."
    )


def test_currently_drifted_block_types_includes_backend_removed_fern_values() -> None:
    """A retired backend block still present in both Fern unions is real drift."""
    drifted = _currently_drifted_block_types(
        backend_values={"navigation"},
        fern_blocks={"navigation", "retired_block"},
        fern_yaml_blocks={"navigation", "retired_block"},
    )

    assert drifted == {"retired_block"}


def test_allowlist_entries_have_required_metadata() -> None:
    """Belt-and-suspenders: every allowlist entry has non-empty ticket/owner/added_at.

    Primary enforcement is at construction via ``BlockDriftEntry.__post_init__``; this
    test is a second layer in case a future refactor relaxes the dataclass. See the
    Skyvern Cloud-only Fern runbook for the allowlist policy.
    """
    for entry in _KNOWN_DRIFT_ALLOWLIST:
        assert entry.block_type, f"allowlist entry missing block_type: {entry!r}"
        assert entry.ticket, f"{entry.block_type!r}: ticket must reference a Linear ticket"
        assert entry.owner, f"{entry.block_type!r}: owner must name a GitHub handle"
        assert isinstance(entry.added_at, date), f"{entry.block_type!r}: added_at must be datetime.date"


def test_block_drift_entry_rejects_missing_metadata() -> None:
    """Construction-time enforcement: empty ticket/owner/block_type must raise."""
    valid_date = date(2026, 4, 23)

    with pytest.raises(ValueError):
        BlockDriftEntry(block_type="", ticket="SKY-1234", owner="@someone", added_at=valid_date)
    with pytest.raises(ValueError):
        BlockDriftEntry(block_type="foo", ticket="", owner="@someone", added_at=valid_date)
    with pytest.raises(ValueError):
        BlockDriftEntry(block_type="foo", ticket="SKY-1234", owner="", added_at=valid_date)
    with pytest.raises(TypeError):
        BlockDriftEntry(block_type="foo", ticket="SKY-1234", owner="@someone", added_at="2026-04-23")  # type: ignore[arg-type]
