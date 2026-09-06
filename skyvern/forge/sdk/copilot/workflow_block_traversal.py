"""Canonical structural traversal for labelled blocks in workflow YAML."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

_NESTED_BLOCK_LIST_KEYS = ("loop_blocks", "blocks")
_BRANCH_LIST_KEYS = ("branch_conditions", "branches", "ordered_branches")

_ValueT = TypeVar("_ValueT")


class _StructureAdapter(Protocol[_ValueT]):
    def mapping_value(self, value: _ValueT, key: str) -> _ValueT | None: ...

    def sequence_items(self, value: _ValueT) -> list[_ValueT] | None: ...

    def label(self, value: _ValueT) -> str | None: ...


@dataclass(frozen=True)
class _StructuralLocation(Generic[_ValueT]):
    value: _ValueT
    owner: _ValueT
    index: int
    path: str


@dataclass(frozen=True)
class WorkflowBlockLocation:
    block: dict[str, Any]
    owner: list[Any]
    index: int
    path: str


@dataclass(frozen=True)
class WorkflowBlockNodeLocation:
    block: MappingNode
    owner: SequenceNode
    index: int
    path: str


class _ParsedAdapter:
    def mapping_value(self, value: Any, key: str) -> Any | None:
        return value.get(key) if isinstance(value, dict) else None

    def sequence_items(self, value: Any) -> list[Any] | None:
        return value if isinstance(value, list) else None

    def label(self, value: Any) -> str | None:
        label = self.mapping_value(value, "label")
        return label if isinstance(label, str) else None


class _NodeAdapter:
    def mapping_value(self, value: Node, key: str) -> Node | None:
        if not isinstance(value, MappingNode):
            return None
        for key_node, child in value.value:
            if isinstance(key_node, ScalarNode) and key_node.value == key:
                return child
        return None

    def sequence_items(self, value: Node) -> list[Node] | None:
        return value.value if isinstance(value, SequenceNode) else None

    def label(self, value: Node) -> str | None:
        label = self.mapping_value(value, "label")
        return label.value if isinstance(label, ScalarNode) else None


def _walk_block_structure(
    sequence: _ValueT,
    adapter: _StructureAdapter[_ValueT],
    path: str,
    *,
    selected_labels: set[str] | None = None,
    inherited: bool = False,
) -> Iterator[_StructuralLocation[_ValueT]]:
    items = adapter.sequence_items(sequence)
    if items is None:
        return
    for index, block in enumerate(items):
        block_path = f"{path}[{index}]"
        selected = inherited or selected_labels is None or adapter.label(block) in selected_labels
        if selected:
            yield _StructuralLocation(value=block, owner=sequence, index=index, path=block_path)
        yield from _walk_block_mapping(
            block,
            adapter,
            block_path,
            selected_labels=selected_labels,
            inherited=selected,
        )


def _walk_block_mapping(
    mapping: _ValueT,
    adapter: _StructureAdapter[_ValueT],
    path: str,
    *,
    selected_labels: set[str] | None,
    inherited: bool,
) -> Iterator[_StructuralLocation[_ValueT]]:
    for key in _NESTED_BLOCK_LIST_KEYS:
        child_sequence = adapter.mapping_value(mapping, key)
        if child_sequence is not None:
            yield from _walk_block_structure(
                child_sequence,
                adapter,
                f"{path}.{key}",
                selected_labels=selected_labels,
                inherited=inherited,
            )
    for key in _BRANCH_LIST_KEYS:
        branches = adapter.mapping_value(mapping, key)
        branch_items = adapter.sequence_items(branches) if branches is not None else None
        if branch_items is None:
            continue
        for index, branch in enumerate(branch_items):
            yield from _walk_block_mapping(
                branch,
                adapter,
                f"{path}.{key}[{index}]",
                selected_labels=selected_labels,
                inherited=inherited,
            )


def workflow_block_locations(
    parsed: dict[str, Any], selected_labels: set[str] | None = None
) -> list[WorkflowBlockLocation]:
    adapter = _ParsedAdapter()
    definition = adapter.mapping_value(parsed, "workflow_definition")
    blocks = adapter.mapping_value(definition, "blocks") if definition is not None else None
    if blocks is None:
        return []
    locations: list[WorkflowBlockLocation] = []
    for location in _walk_block_structure(
        blocks,
        adapter,
        "workflow_definition.blocks",
        selected_labels=selected_labels,
    ):
        if isinstance(location.value, dict) and isinstance(location.owner, list):
            locations.append(
                WorkflowBlockLocation(
                    block=location.value,
                    owner=location.owner,
                    index=location.index,
                    path=location.path,
                )
            )
    return locations


def workflow_block_node_locations(root: Node | None) -> list[WorkflowBlockNodeLocation]:
    if root is None:
        return []
    adapter = _NodeAdapter()
    definition = adapter.mapping_value(root, "workflow_definition")
    blocks = adapter.mapping_value(definition, "blocks") if definition is not None else None
    if blocks is None:
        return []
    locations: list[WorkflowBlockNodeLocation] = []
    for location in _walk_block_structure(blocks, adapter, "workflow_definition.blocks"):
        if isinstance(location.value, MappingNode) and isinstance(location.owner, SequenceNode):
            locations.append(
                WorkflowBlockNodeLocation(
                    block=location.value,
                    owner=location.owner,
                    index=location.index,
                    path=location.path,
                )
            )
    return locations


def workflow_link_node_mappings(root: Node | None) -> list[MappingNode]:
    """Return only block and branch mappings that may own workflow graph links."""
    if root is None:
        return []
    adapter = _NodeAdapter()
    definition = adapter.mapping_value(root, "workflow_definition")
    blocks = adapter.mapping_value(definition, "blocks") if definition is not None else None
    block_items = adapter.sequence_items(blocks) if blocks is not None else None
    if block_items is None:
        return []

    mappings: list[MappingNode] = []
    visited: set[int] = set()

    def visit_children(mapping: MappingNode) -> None:
        for key in _NESTED_BLOCK_LIST_KEYS:
            sequence = adapter.mapping_value(mapping, key)
            children = adapter.sequence_items(sequence) if sequence is not None else None
            for child in children or []:
                if isinstance(child, MappingNode):
                    visit_block(child)
        for key in _BRANCH_LIST_KEYS:
            branches = adapter.mapping_value(mapping, key)
            branch_items = adapter.sequence_items(branches) if branches is not None else None
            for branch in branch_items or []:
                if not isinstance(branch, MappingNode) or id(branch) in visited:
                    continue
                visited.add(id(branch))
                mappings.append(branch)
                visit_children(branch)

    def visit_block(block: MappingNode) -> None:
        if id(block) in visited:
            return
        visited.add(id(block))
        mappings.append(block)
        visit_children(block)

    for block in block_items:
        if isinstance(block, MappingNode):
            visit_block(block)
    return mappings
