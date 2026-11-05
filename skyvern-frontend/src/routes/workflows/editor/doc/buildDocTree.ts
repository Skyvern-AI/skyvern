import type { Edge, Node } from "@xyflow/react";
import {
  BranchCriteriaTypes,
  type BranchCondition,
} from "../../types/workflowTypes";
import {
  edgeBelongsToScope,
  findScopeStartNode,
  getOrderedBlockIdsAtScope,
  type SortableBlockScopeDescriptor,
} from "../sortable/scope";
import type { AppNode } from "../nodes";
import { graphNoteReadings } from "./noteSequences";

export type DocNoteItem = {
  kind: "note";
  ownerNodeId: string;
  placement: "above" | "trailing";
  text: string;
};
export type DocBranch = {
  branchId: string;
  kind: "if" | "elseIf" | "otherwise";
  // An Otherwise the definition doesn't have; shown so every conditional reads the same way.
  synthetic: boolean;
  condition: string;
  check: "ai" | "exact" | null;
  children: DocItem[];
  stepCount: number;
  jumpTo?: string;
};
type DocPlacement = {
  readOnlyReason?: string;
  finally?: boolean;
  reachedFrom?: number;
};
export type DocItem =
  | DocNoteItem
  | (DocPlacement &
      (
        | { kind: "step"; nodeId: string }
        | { kind: "loop"; nodeId: string; children: DocItem[] }
        | { kind: "conditional"; nodeId: string; branches: DocBranch[] }
        | { kind: "unplaced"; nodeIds: string[] }
      ));
export type DocTree = { items: DocItem[] };

const isBlock = (node: Node) =>
  node.type !== "start" && node.type !== "nodeAdder";
const UNPLACED =
  "These steps can't be placed in the document. Open Visual to see how they connect.";
const INVALID_SECTION =
  "This part has connections the document can't show. Open Visual to see it.";
const INVALID_CONDITIONAL =
  "This conditional has branch connections the document can't show. Open Visual to see it.";

const text = (value: unknown) =>
  typeof value === "string" && value.trim() ? value : null;

function branchCheck(branch: BranchCondition): DocBranch["check"] {
  if (branch.is_default || !branch.criteria?.expression?.trim()) return null;
  return branch.criteria.criteria_type === BranchCriteriaTypes.Jinja2Template
    ? "exact"
    : "ai";
}

export function countDocSteps(items: DocItem[]): number {
  return items.reduce((count, item) => {
    if (item.kind === "note") return count;
    if (item.kind === "unplaced") return count + item.nodeIds.length;
    const nested =
      item.kind === "loop"
        ? countDocSteps(item.children)
        : item.kind === "conditional"
          ? item.branches.reduce((n, b) => n + countDocSteps(b.children), 0)
          : 0;
    return count + 1 + nested;
  }, 0);
}

/**
 * The note texts each step reads at run time, keyed by node id. Canvas markers and the Doc's
 * "reads this" lines both use it, so they match the run (see noteSequences.ts).
 */
export function getDocNotesByNodeId(
  nodes: Node[],
  edges: Edge[],
  finallyBlockLabel: string | null = null,
): Map<string, Array<string>> {
  const { textsByLabel } = graphNoteReadings(
    nodes as AppNode[],
    edges,
    finallyBlockLabel,
  );
  const result = new Map<string, Array<string>>();
  for (const node of nodes) {
    const label = (node.data as { label?: unknown } | undefined)?.label;
    const texts = typeof label === "string" ? textsByLabel.get(label) : null;
    if (texts) result.set(node.id, texts);
  }
  return result;
}

/** A read-only projection of the live graph: never repairs or writes to it. */
export function buildDocTree(
  nodes: Node[],
  edges: Edge[],
  finallyBlockLabel: string | null = null,
): DocTree {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const placed = new Set<string>();
  const belongs = (node: Node, scope: SortableBlockScopeDescriptor) =>
    (node.parentId ?? null) === scope.parentId &&
    (node.data.conditionalBranchId ?? null) === scope.conditionalBranchId &&
    (scope.conditionalBranchId === null
      ? !node.data.conditionalNodeId
      : node.data.conditionalNodeId === scope.parentId);

  // `shared` holds steps reached from more than one branch of the scope's conditional:
  // they may sit in a sibling branch and carry that branch's edges too.
  function inspectScope(
    scope: SortableBlockScopeDescriptor,
    shared: ReadonlySet<string> = new Set(),
  ) {
    const ids = getOrderedBlockIdsAtScope({ nodes, edges, scope });
    const start = findScopeStartNode(nodes, scope);
    const adder = nodes.find(
      (n) => n.type === "nodeAdder" && (n.parentId ?? null) === scope.parentId,
    );
    const chain = [start?.id, ...ids, adder?.id];
    const chainEdges: Edge[] = [];
    let valid =
      Boolean(start && adder) &&
      ids.every((id) => {
        const node = byId.get(id);
        return node && (shared.has(id) || belongs(node, scope));
      });
    for (let i = 0; i < chain.length - 1; i++) {
      const outgoing = edges.filter(
        (e) => e.source === chain[i] && edgeBelongsToScope(e, scope),
      );
      const edge = outgoing[0];
      if (
        outgoing.length !== 1 ||
        !edge ||
        edge.target !== chain[i + 1] ||
        (scope.conditionalBranchId !== null &&
          edge.data?.conditionalNodeId !== scope.parentId)
      ) {
        valid = false;
      } else {
        chainEdges.push(edge);
      }
    }
    const members = nodes.filter(
      (node) => isBlock(node) && belongs(node, scope),
    );
    if (members.some((node) => !ids.includes(node.id))) valid = false;
    // Extra incoming or cross-scope edges make the order ambiguous. A shared step's
    // edges in sibling branches are checked when those branches are inspected.
    const memberIds = new Set(ids);
    const siblingBranchEdge = (edge: Edge) =>
      scope.conditionalBranchId !== null &&
      edge.data?.conditionalNodeId === scope.parentId &&
      (shared.has(edge.source) || shared.has(edge.target));
    if (
      edges.some(
        (edge) =>
          (memberIds.has(edge.target) || memberIds.has(edge.source)) &&
          !chainEdges.includes(edge) &&
          !siblingBranchEdge(edge),
      )
    )
      valid = false;
    return { ids, valid };
  }

  function ownedDescendants(nodeId: string): Node[] {
    const owned = new Set([nodeId]);
    for (let size = -1; size !== owned.size; ) {
      size = owned.size;
      for (const node of nodes) {
        if (
          (node.parentId && owned.has(node.parentId)) ||
          node.data.conditionalNodeId === nodeId
        )
          owned.add(node.id);
      }
    }
    return nodes.filter(
      (node) => node.id !== nodeId && owned.has(node.id) && isBlock(node),
    );
  }

  // Steps reached from several branches form a common tail of each branch chain that
  // reaches them. Returns null when the branches don't have that shape.
  function sharedTail(
    chains: Map<string, string[]>,
  ): { tail: string[]; counts: Map<string, number> } | null {
    const counts = new Map<string, number>();
    for (const ids of chains.values())
      for (const id of new Set(ids)) counts.set(id, (counts.get(id) ?? 0) + 1);
    let tail: string[] = [];
    for (const ids of chains.values()) {
      const first = ids.findIndex((id) => (counts.get(id) ?? 0) > 1);
      if (first === -1) continue;
      const rest = ids.slice(first);
      if (rest.length > tail.length) tail = rest;
    }
    for (const ids of chains.values()) {
      const first = ids.findIndex((id) => (counts.get(id) ?? 0) > 1);
      if (first === -1) continue;
      const rest = ids.slice(first);
      const offset = tail.length - rest.length;
      if (rest.some((id, i) => tail[offset + i] !== id)) return null;
    }
    return { tail, counts };
  }

  function conditionalShapeIsValid(
    node: Node,
    branches: BranchCondition[],
  ): boolean {
    if (
      !branches.length ||
      new Set(branches.map((b) => b.id)).size !== branches.length
    )
      return false;
    const fromNode = edges.filter((edge) => edge.source === node.id);
    const real = fromNode.filter((edge) => !edge.data?.synthetic);
    // A trailing conditional reaches the finally step only through the display-only edge.
    const outgoing = real.length ? real : fromNode;
    if (outgoing.length !== 1) return false;
    const next = byId.get(outgoing[0]!.target);
    if (
      !next ||
      next.type === "start" ||
      (next.parentId ?? null) !== (node.parentId ?? null) ||
      (isBlock(next) &&
        !belongs(next, {
          parentId: node.parentId ?? null,
          conditionalBranchId: (node.data.conditionalBranchId ?? null) as
            | string
            | null,
        }))
    )
      return false;
    const directMembers = nodes.filter(
      (n) =>
        isBlock(n) &&
        (n.parentId === node.id || n.data.conditionalNodeId === node.id),
    );
    if (
      directMembers.some(
        (n) =>
          n.parentId !== node.id ||
          n.data.conditionalNodeId !== node.id ||
          !branches.some((b) => b.id === n.data.conditionalBranchId),
      )
    )
      return false;
    return !edges.some(
      (edge) =>
        edge.data?.conditionalNodeId === node.id &&
        !branches.some((b) => b.id === edge.data?.conditionalBranchId),
    );
  }

  function withNotes(node: Node, item: DocItem): DocItem[] {
    const above = text(node.data.notes);
    const trailing = text(node.data.trailingNotes);
    return [
      ...(above
        ? [
            {
              kind: "note",
              ownerNodeId: node.id,
              placement: "above",
              text: above,
            } as const,
          ]
        : []),
      item,
      ...(trailing
        ? [
            {
              kind: "note",
              ownerNodeId: node.id,
              placement: "trailing",
              text: trailing,
            } as const,
          ]
        : []),
    ];
  }

  function unplacedFor(missing: Node[]): DocItem {
    for (const n of missing) placed.add(n.id);
    return {
      kind: "unplaced",
      nodeIds: missing.map((n) => n.id),
      readOnlyReason: UNPLACED,
    };
  }

  function placeConditional(node: Node, reason?: string): DocItem[] {
    const id = node.id;
    const definitions = (node.data.branches ?? []) as BranchCondition[];
    let conditionalReason =
      reason ??
      (conditionalShapeIsValid(node, definitions)
        ? undefined
        : INVALID_CONDITIONAL);
    const scopeOf = (branchId: string) => ({
      parentId: id,
      conditionalBranchId: branchId,
    });
    const chains = new Map(
      definitions.map((b) => [
        b.id,
        getOrderedBlockIdsAtScope({ nodes, edges, scope: scopeOf(b.id) }),
      ]),
    );
    const rejoin = sharedTail(chains);
    if (!rejoin) conditionalReason ??= INVALID_CONDITIONAL;
    const shared = new Set(rejoin?.tail ?? []);
    if (
      !conditionalReason &&
      definitions.some((b) => !inspectScope(scopeOf(b.id), shared).valid)
    )
      conditionalReason = INVALID_CONDITIONAL;

    const ordered = [
      ...definitions.filter((b) => !b.is_default),
      ...definitions.filter((b) => b.is_default),
    ];
    let conditionIndex = 0;
    const branches: DocBranch[] = ordered.map((definition) => {
      const chain = chains.get(definition.id) ?? [];
      const firstShared = chain.findIndex((nodeId) => shared.has(nodeId));
      const own = firstShared === -1 ? chain : chain.slice(0, firstShared);
      const children = placeSequence(own, scopeOf(definition.id), {
        reason: conditionalReason,
      });
      return {
        branchId: definition.id,
        kind: definition.is_default
          ? "otherwise"
          : conditionIndex++ === 0
            ? "if"
            : "elseIf",
        synthetic: false,
        condition:
          definition.criteria?.expression?.trim() ||
          definition.description?.trim() ||
          "",
        check: branchCheck(definition),
        children,
        stepCount: countDocSteps(children),
        ...(firstShared !== -1 && { jumpTo: chain[firstShared] }),
      };
    });
    if (!branches.some((b) => b.kind === "otherwise"))
      branches.push({
        branchId: `${id}:otherwise`,
        kind: "otherwise",
        synthetic: true,
        condition: "",
        check: null,
        children: [],
        stepCount: 0,
      });
    const tailItems = placeSequence(rejoin?.tail ?? [], null, {
      reason: conditionalReason,
      reachedFrom: rejoin?.counts,
    });
    const missing = ownedDescendants(id).filter((n) => !placed.has(n.id));
    const item: DocItem = {
      kind: "conditional",
      nodeId: id,
      branches,
      readOnlyReason:
        conditionalReason ?? (missing.length ? INVALID_CONDITIONAL : undefined),
    };
    return [
      ...withNotes(node, item),
      ...tailItems,
      ...(missing.length ? [unplacedFor(missing)] : []),
    ];
  }

  function placeNode(
    node: Node,
    reason: string | undefined,
    flags: Pick<DocPlacement, "finally" | "reachedFrom">,
  ): DocItem[] {
    const id = node.id;
    placed.add(id);
    if (node.type === "conditional") {
      const [first, ...rest] = placeConditional(node, reason);
      // withNotes may put a note first; the flags belong on the conditional itself.
      return [first!, ...rest].map((item) =>
        item.kind === "conditional" ? { ...item, ...flags } : item,
      );
    }
    if (node.type === "loop") {
      const children = walkScope(
        { parentId: id, conditionalBranchId: null },
        reason,
      );
      const missing = ownedDescendants(id).filter((n) => !placed.has(n.id));
      if (missing.length) children.push(unplacedFor(missing));
      return withNotes(node, {
        kind: "loop",
        nodeId: id,
        children,
        readOnlyReason: reason ?? (missing.length ? UNPLACED : undefined),
        ...flags,
      });
    }
    return withNotes(node, {
      kind: "step",
      nodeId: id,
      readOnlyReason: reason,
      ...flags,
    });
  }

  function placeSequence(
    ids: string[],
    scope: SortableBlockScopeDescriptor | null,
    options: { reason?: string; reachedFrom?: Map<string, number> },
  ): DocItem[] {
    const items: DocItem[] = [];
    for (const id of ids) {
      const node = byId.get(id);
      if (!node || !isBlock(node) || placed.has(id)) continue;
      if (scope && !belongs(node, scope)) continue;
      const reachedFrom = options.reachedFrom?.get(id);
      items.push(
        ...placeNode(node, options.reason, {
          ...(scope?.parentId === null &&
            finallyBlockLabel !== null &&
            node.data.label === finallyBlockLabel && { finally: true }),
          ...(reachedFrom && reachedFrom > 1 && { reachedFrom }),
        }),
      );
    }
    return items;
  }

  function walkScope(
    scope: SortableBlockScopeDescriptor,
    inheritedReason?: string,
  ): DocItem[] {
    const inspected = inspectScope(scope);
    const reason =
      inheritedReason ?? (inspected.valid ? undefined : INVALID_SECTION);
    return placeSequence(inspected.ids, scope, { reason });
  }

  const items = walkScope({ parentId: null, conditionalBranchId: null });
  const missing = nodes.filter((node) => isBlock(node) && !placed.has(node.id));
  if (missing.length) items.push(unplacedFor(missing));
  return { items };
}
