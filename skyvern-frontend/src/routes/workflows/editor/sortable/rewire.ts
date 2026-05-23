import { arrayMove } from "@dnd-kit/sortable";
import type { Edge } from "@xyflow/react";

import {
  edgeBelongsToScope,
  findScopeStartNode,
  getOrderedBlockIdsAtScope,
  type SortableBlockScopeDescriptor,
  type SortableScopeNode,
} from "./scope";

export type BlockDropRewireResult = {
  edges: Array<Edge>;
  newOrder: Array<string>;
};

// `kind` strings on the matching variants of `DropBlockedReason`
// (./dropBlockedToast.tsx) must stay in sync — FlowRenderer maps between
// the two at the drop-end boundary.
export type BlockDropBlockedReason =
  | "finally-pin"
  | "cross-scope"
  | "chain-mismatch";

export type BlockDropOutcome =
  | { kind: "ok"; edges: Array<Edge>; newOrder: Array<string> }
  | { kind: "blocked"; reason: BlockDropBlockedReason }
  | { kind: "noop" };

type BlockDropRewireInput = {
  nodes: Array<SortableScopeNode>;
  edges: Array<Edge>;
  scope: SortableBlockScopeDescriptor;
  activeId: string;
  overId: string | null;
  // When set, `finallyBlockId` is the top-level node id whose tail slot is
  // immovable; any drag of that block, or any drop that would push it
  // backward, is refused as `finally-pin`. Only the top-level scope honors
  // this — nested scopes ignore it because `finallyBlockLabel` is a
  // workflow-root concept.
  finallyBlockId?: string | null;
};

// Pure classifier — no React state. Callers can run it, feed the result
// into doLayout, and flip hasChanges in a single render pass.
export function classifyBlockDrop({
  nodes,
  edges,
  scope,
  activeId,
  overId,
  finallyBlockId = null,
}: BlockDropRewireInput): BlockDropOutcome {
  const order = getOrderedBlockIdsAtScope({ nodes, edges, scope });
  if (order.length === 0) return { kind: "noop" };

  const oldIndex = order.indexOf(activeId);
  if (oldIndex < 0) return { kind: "noop" };

  if (overId === null || overId === activeId) return { kind: "noop" };

  const newIndex = order.indexOf(overId);
  if (newIndex < 0) {
    // If the over id isn't a workflow block at all (e.g. a future toolbar
    // droppable), don't surface the cross-scope toast — silently no-op.
    const overIsKnownNode = nodes.some((n) => n.id === overId);
    if (!overIsKnownNode) return { kind: "noop" };
    return { kind: "blocked", reason: "cross-scope" };
  }
  if (oldIndex === newIndex) return { kind: "noop" };

  // Finally-block gate (top-level only): refuse drags of the finally
  // block itself or drops that would push it backward.
  const isTopLevelScope =
    scope.parentId === null && scope.conditionalBranchId === null;
  if (isTopLevelScope && finallyBlockId) {
    const finallyIndex = order.indexOf(finallyBlockId);
    if (finallyIndex >= 0) {
      if (activeId === finallyBlockId) {
        return { kind: "blocked", reason: "finally-pin" };
      }
      if (newIndex >= finallyIndex) {
        return { kind: "blocked", reason: "finally-pin" };
      }
    }
  }

  const newOrder = arrayMove(order, oldIndex, newIndex);

  const startNode = findScopeStartNode(nodes, scope);
  if (!startNode) return { kind: "noop" };
  const startId = startNode.id;

  // Scope-aware filter: for branch scopes, the conditional's NodeAdder is
  // shared across branches via parallel tail edges, so we have to pick the
  // edge that belongs to *this* branch.
  const lastSibling = order[order.length - 1]!;
  const afterEdge = edges.find(
    (edge) => edge.source === lastSibling && edgeBelongsToScope(edge, scope),
  );
  if (!afterEdge) return { kind: "noop" };
  const afterId = afterEdge.target;

  const oldChainPairs = buildChainPairs(startId, order, afterId);
  const newChainPairs = buildChainPairs(startId, newOrder, afterId);

  const oldChainEdges: Array<Edge> = [];
  for (const [src, dst] of oldChainPairs) {
    const edge = edges.find(
      (e) =>
        e.source === src && e.target === dst && edgeBelongsToScope(e, scope),
    );
    if (edge) oldChainEdges.push(edge);
  }
  // A reorder preserves hop count, so newChainPairs and oldChainEdges must
  // be the same length. If they aren't, the existing chain is malformed
  // (a missing edge in the graph) and we'd dereference an undefined
  // template below; bail out as a no-op so the rewire path stays safe.
  if (oldChainEdges.length !== oldChainPairs.length) {
    if (process.env.NODE_ENV === "development") {
      console.warn(
        "[sortable/rewire] chain-edge count mismatch (expected " +
          `${oldChainPairs.length}, found ${oldChainEdges.length}); drop refused. ` +
          "Likely a missing edge in the graph from an upstream mutation.",
        { scope, activeId, afterId },
      );
    }
    return { kind: "blocked", reason: "chain-mismatch" };
  }

  const oldChainEdgeIds = new Set(oldChainEdges.map((e) => e.id));
  const nonChainEdges = edges.filter((e) => !oldChainEdgeIds.has(e.id));

  const newChainEdges: Array<Edge> = newChainPairs.map(([src, dst], i) => {
    // Reuse the existing edge object at the same chain position so React
    // Flow keys, styles, and conditional metadata survive the drop.
    const template = oldChainEdges[i]!;
    const type = dst === afterId ? "default" : "edgeWithAddButton";
    return {
      ...template,
      source: src,
      target: dst,
      type,
    };
  });

  return {
    kind: "ok",
    edges: [...nonChainEdges, ...newChainEdges],
    newOrder,
  };
}

// Returns null when the drop is a no-op or blocked. Callers that need to
// distinguish the two (to fire the toast) should call classifyBlockDrop
// directly — this wrapper is kept for tests / callers that only consume
// the success path.
export function rewireBlockDropInScope(
  input: BlockDropRewireInput,
): BlockDropRewireResult | null {
  const outcome = classifyBlockDrop(input);
  if (outcome.kind !== "ok") return null;
  return { edges: outcome.edges, newOrder: outcome.newOrder };
}

function buildChainPairs(
  startId: string,
  order: Array<string>,
  afterId: string,
): Array<[string, string]> {
  const pairs: Array<[string, string]> = [];
  pairs.push([startId, order[0]!]);
  for (let i = 0; i < order.length - 1; i++) {
    pairs.push([order[i]!, order[i + 1]!]);
  }
  pairs.push([order[order.length - 1]!, afterId]);
  return pairs;
}
