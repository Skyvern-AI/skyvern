"""Parity guard between the backend BlockType enum and the Fern SDK unions.

The vendored Fern SDK at ``skyvern/client/`` is regenerated manually. When a
new block type is added to ``skyvern/schemas/workflows.BlockType`` without
regenerating the SDK, MCP read paths that still deserialize through the Fern
``Workflow`` type break (see ``test_mcp_workflow_list_drift``). Even though the
MCP tools now bypass Fern for Workflow reads, we still want an early warning
when a regeneration is overdue so downstream clients that *do* use the Fern
SDK directly stay in sync.

This test introspects both Fern discriminated unions and diffs them against
the backend enum. Known-drifted values are tolerated via an allowlist with a
Linear tracking pointer to the regeneration task; any NEW drift fails CI.
"""

from __future__ import annotations

import typing

import pytest

from skyvern.client.types.workflow_definition_blocks_item import WorkflowDefinitionBlocksItem
from skyvern.client.types.workflow_definition_yaml_blocks_item import WorkflowDefinitionYamlBlocksItem
from skyvern.schemas.workflows import BlockType

# Known drift: block types present in the backend but not yet regenerated into
# the vendored Fern SDK. Run `fern generate` (or the equivalent Skyvern SDK
# regen workflow) to resync, then remove the entry here.
# Tracked follow-up: SKY-9227
# https://linear.app/skyvern/issue/SKY-9227/prevent-fern-sdk-drift-from-breaking-workflow-block-types
# Remove this allowlist after the Fern SDK has been regenerated to include
# these values and downstream `skyvern` PyPI + `@skyvern/client` npm packages
# are published.
_KNOWN_DRIFT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "google_sheets_read",
        "google_sheets_write",
        # SKY-8771: Fern SDK regeneration for the new while_loop block lands in PR 2.
        "while_loop",
    }
)


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


def test_backend_block_types_present_in_fern_unions() -> None:
    """Every BlockType value must be known to both Fern unions (or allowlisted)."""
    backend_values = {member.value for member in BlockType}

    fern_blocks = _fern_union_block_types(WorkflowDefinitionBlocksItem)
    fern_yaml_blocks = _fern_union_block_types(WorkflowDefinitionYamlBlocksItem)
    fern_known = fern_blocks & fern_yaml_blocks

    missing_in_fern = backend_values - fern_known - _KNOWN_DRIFT_ALLOWLIST
    assert not missing_in_fern, (
        "Fern SDK drift detected: the backend `skyvern.schemas.workflows.BlockType` "
        f"has value(s) {sorted(missing_in_fern)!r} that the Fern discriminated unions "
        "(`WorkflowDefinitionBlocksItem` / `WorkflowDefinitionYamlBlocksItem`) do not know. "
        "Run `fern generate` to resync the vendored SDK at skyvern/client/, or add the "
        "value to _KNOWN_DRIFT_ALLOWLIST in this test if the drift is intentional "
        "and tracked."
    )


def test_fern_union_variants_are_subset_of_backend_or_deprecated() -> None:
    """Fern may legitimately retain retired types during a deprecation window.

    This direction is informational: if Fern references a block_type the backend
    no longer emits, we note it but don't fail — clients using a new SDK against
    an older backend is the usual deprecation trajectory.
    """
    backend_values = {member.value for member in BlockType}

    fern_blocks = _fern_union_block_types(WorkflowDefinitionBlocksItem)
    fern_yaml_blocks = _fern_union_block_types(WorkflowDefinitionYamlBlocksItem)

    extras = (fern_blocks | fern_yaml_blocks) - backend_values
    # Assert parity but allow the allowlist to flex in either direction so a
    # retired block type stays tolerated for one regen cycle.
    unexpected = extras - _KNOWN_DRIFT_ALLOWLIST
    assert not unexpected, (
        f"Fern unions reference block_type value(s) {sorted(unexpected)!r} that are "
        "not in the backend BlockType enum. If this is a planned deprecation, add "
        "the value to _KNOWN_DRIFT_ALLOWLIST; otherwise investigate."
    )


def test_allowlist_entries_are_actually_drifted() -> None:
    """Keeps _KNOWN_DRIFT_ALLOWLIST honest: drop stale entries after Fern regen."""
    backend_values = {member.value for member in BlockType}

    fern_blocks = _fern_union_block_types(WorkflowDefinitionBlocksItem)
    fern_yaml_blocks = _fern_union_block_types(WorkflowDefinitionYamlBlocksItem)
    fern_known = fern_blocks & fern_yaml_blocks

    currently_drifted = (backend_values - fern_known) | (fern_blocks ^ fern_yaml_blocks)
    stale_allowlist_entries = _KNOWN_DRIFT_ALLOWLIST - currently_drifted
    assert not stale_allowlist_entries, (
        f"_KNOWN_DRIFT_ALLOWLIST contains stale entries {sorted(stale_allowlist_entries)!r} "
        "that no longer drift. Remove them to keep the allowlist tight."
    )
