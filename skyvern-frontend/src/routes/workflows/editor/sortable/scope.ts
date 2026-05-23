import type { Edge } from "@xyflow/react";

// A scope identifies a set of sibling blocks that can be reordered among
// themselves: top level, inside a loop, or inside a single conditional
// branch. The descriptor is keyed so all three live in one walker.
export type SortableBlockScopeDescriptor = {
  parentId: string | null;
  conditionalBranchId: string | null;
};

// Kept independent of the AppNode union so this module and its tests
// don't transitively import the full node registry.
export type SortableScopeNode = {
  id: string;
  type?: string;
  parentId?: string;
};

export const TOP_LEVEL_SCOPE: SortableBlockScopeDescriptor = {
  parentId: null,
  conditionalBranchId: null,
};

export const TOP_LEVEL_SCOPE_KEY = "scope:top-level";

export function getScopeKey(scope: SortableBlockScopeDescriptor): string {
  if (scope.parentId === null && scope.conditionalBranchId === null) {
    return TOP_LEVEL_SCOPE_KEY;
  }
  return `scope:${scope.parentId ?? "__root__"}:${scope.conditionalBranchId ?? "__main__"}`;
}

function isSortableBlock(node: SortableScopeNode): boolean {
  return node.type !== "start" && node.type !== "nodeAdder";
}

function isTerminalWalkStop(node: SortableScopeNode): boolean {
  return node.type === "nodeAdder" || node.type === "start";
}

// Top-level and loop scopes have edges with no conditionalBranchId;
// conditional-branch scopes tag every edge with one. The filter lets a
// single walker pick the correct outgoing edge at a fan-out node.
export function edgeBelongsToScope(
  edge: Edge,
  scope: SortableBlockScopeDescriptor,
): boolean {
  const edgeData = edge.data as
    | { conditionalBranchId?: string | null }
    | undefined;
  const edgeBranchId = edgeData?.conditionalBranchId ?? null;
  if (scope.conditionalBranchId === null) {
    return edgeBranchId === null;
  }
  return edgeBranchId === scope.conditionalBranchId;
}

// All branches under one conditional share the same start node; the
// per-branch differentiation happens at edge-filter time.
export function findScopeStartNode(
  nodes: Array<SortableScopeNode>,
  scope: SortableBlockScopeDescriptor,
): SortableScopeNode | null {
  // A null parentId with a non-null branch id is a malformed descriptor —
  // the top-level chain has no branches.
  if (scope.parentId === null && scope.conditionalBranchId !== null) {
    return null;
  }
  const startNode = nodes.find((node) => {
    if (node.type !== "start") return false;
    const nodeParentId = node.parentId ?? null;
    return nodeParentId === scope.parentId;
  });
  return startNode ?? null;
}

export function collectLoopScopes(
  nodes: Array<SortableScopeNode>,
): Array<SortableBlockScopeDescriptor> {
  return nodes
    .filter((node) => node.type === "loop")
    .map((node) => ({ parentId: node.id, conditionalBranchId: null }));
}

// Branch ids come from edge data, so an empty branch (only START → adder)
// still produces a scope; that keeps its SortableContext mounted before
// any block is dropped into it.
export function collectConditionalBranchScopes(
  nodes: Array<SortableScopeNode>,
  edges: Array<Edge>,
): Array<SortableBlockScopeDescriptor> {
  const conditionalIds = new Set(
    nodes.filter((node) => node.type === "conditional").map((node) => node.id),
  );
  if (conditionalIds.size === 0) return [];

  const branchesByConditional = new Map<string, Set<string>>();
  for (const edge of edges) {
    const edgeData = edge.data as
      | { conditionalNodeId?: string; conditionalBranchId?: string }
      | undefined;
    const conditionalNodeId = edgeData?.conditionalNodeId;
    const conditionalBranchId = edgeData?.conditionalBranchId;
    if (!conditionalNodeId || !conditionalBranchId) continue;
    if (!conditionalIds.has(conditionalNodeId)) continue;
    let branchSet = branchesByConditional.get(conditionalNodeId);
    if (!branchSet) {
      branchSet = new Set();
      branchesByConditional.set(conditionalNodeId, branchSet);
    }
    branchSet.add(conditionalBranchId);
  }

  const scopes: Array<SortableBlockScopeDescriptor> = [];
  for (const [parentId, branchIds] of branchesByConditional) {
    for (const conditionalBranchId of branchIds) {
      scopes.push({ parentId, conditionalBranchId });
    }
  }
  return scopes;
}

// Walk the edge chain from the scope's start node and return ordered
// sibling ids. Branch scopes share a start node, so the walker stays in
// its own branch via edgeBelongsToScope.
export function getOrderedBlockIdsAtScope({
  nodes,
  edges,
  scope,
}: {
  nodes: Array<SortableScopeNode>;
  edges: Array<Edge>;
  scope: SortableBlockScopeDescriptor;
}): Array<string> {
  const startNode = findScopeStartNode(nodes, scope);
  if (!startNode) return [];

  const ids: Array<string> = [];
  const visited = new Set<string>();
  let nextId: string | undefined = edges.find(
    (edge) => edge.source === startNode.id && edgeBelongsToScope(edge, scope),
  )?.target;

  while (nextId && !visited.has(nextId)) {
    visited.add(nextId);
    const node = nodes.find((n) => n.id === nextId);
    if (!node) break;
    if (isTerminalWalkStop(node)) break;
    if (!isSortableBlock(node)) {
      // Today this branch only fires for `start` / `nodeAdder`, which are
      // already broken out by `isTerminalWalkStop` above — but if a
      // future non-block, non-terminal node type lands inside a chain,
      // dropping the `edgeBelongsToScope` filter would silently route
      // the walker into a sibling branch. Keep the filter as a safety
      // net so the walker stays scope-confined.
      nextId = edges.find(
        (edge) => edge.source === nextId && edgeBelongsToScope(edge, scope),
      )?.target;
      continue;
    }
    ids.push(nextId);
    const currentId = nextId;
    nextId = edges.find(
      (edge) => edge.source === currentId && edgeBelongsToScope(edge, scope),
    )?.target;
  }

  return ids;
}
