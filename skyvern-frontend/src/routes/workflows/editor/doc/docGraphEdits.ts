import type { Edge } from "@xyflow/react";
import { nanoid } from "nanoid";
import type { BranchContext } from "@/store/WorkflowPanelStore";
import type { BranchCondition } from "../../types/workflowTypes";
import {
  containsJinjaReference,
  objectContainsJinjaReference,
  replaceJinjaReference,
  SKIP_KEYS_FOR_JINJA_CHECK,
} from "../jinjaReferences";
import {
  isWorkflowBlockNode,
  type AppNode,
  type WorkflowBlockNode,
} from "../nodes";
import { LOOP_RESERVED_PARAMETERS } from "../constants";
import {
  edgeBelongsToScope,
  findScopeStartNode,
  type SortableBlockScopeDescriptor,
} from "../sortable/scope";
import {
  createNode,
  descendants,
  getOutputParameterKey,
  getLabelOutputParameterKeyCollision,
  removeJinjaReferenceFromNodes,
  removeKeyFromNodesParameterKeys,
  getUpdatedNodesAfterLabelUpdateForParameterKeys,
  updateNodeAndDescendantsVisibility,
} from "../workflowEditorUtils";
import { getDocProseFields } from "./docStepRegistry";

export type DocScopeRef = SortableBlockScopeDescriptor;
export type DocGraph = { nodes: AppNode[]; edges: Edge[] };

const isBlock = (node: AppNode) =>
  node.type !== "start" && node.type !== "nodeAdder";

export function scopeOfNode(node: AppNode): DocScopeRef {
  const branchId = isWorkflowBlockNode(node)
    ? (node.data.conditionalBranchId ?? null)
    : null;
  const inConditional =
    isWorkflowBlockNode(node) &&
    Boolean(node.data.conditionalNodeId) &&
    node.data.conditionalNodeId === node.parentId;
  return {
    parentId: node.parentId ?? null,
    conditionalBranchId: inConditional ? branchId : null,
  };
}

const sameScope = (a: DocScopeRef, b: DocScopeRef) =>
  a.parentId === b.parentId && a.conditionalBranchId === b.conditionalBranchId;

type Chain = {
  startId: string;
  // Movable blocks, in order. A top-level finally block is pinned and left out.
  ids: string[];
  // The node the last block points at: the scope's adder, or the pinned finally block.
  endId: string;
  edges: Edge[];
};

function edgeInScope(edge: Edge, scope: DocScopeRef) {
  if (!edgeBelongsToScope(edge, scope)) return false;
  return (
    scope.conditionalBranchId === null ||
    (edge.data as { conditionalNodeId?: string } | undefined)
      ?.conditionalNodeId === scope.parentId
  );
}

/** The scope's start → blocks → adder chain, or null when it isn't a simple chain. */
export function readChain(
  graph: DocGraph,
  scope: DocScopeRef,
  finallyId: string | null = null,
): Chain | null {
  const start = findScopeStartNode(graph.nodes, scope);
  if (!start) return null;
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const ids: string[] = [];
  const chainEdges: Edge[] = [];
  const seen = new Set<string>();
  let current = start.id;
  for (;;) {
    const outgoing = graph.edges.filter(
      (e) => e.source === current && edgeInScope(e, scope),
    );
    if (outgoing.length !== 1) return null;
    const edge = outgoing[0]!;
    const next = byId.get(edge.target);
    if (!next || seen.has(next.id)) return null;
    seen.add(next.id);
    chainEdges.push(edge);
    if (next.type === "nodeAdder") {
      return { startId: start.id, ids, endId: next.id, edges: chainEdges };
    }
    if (next.type === "start") return null;
    if (scope.parentId === null && next.id === finallyId) {
      return { startId: start.id, ids, endId: next.id, edges: chainEdges };
    }
    ids.push(next.id);
    current = next.id;
  }
}

function chainEdgesFor(
  chain: Chain,
  ids: string[],
  scope: DocScopeRef,
  byId: Map<string, AppNode>,
): Edge[] {
  const order = [chain.startId, ...ids, chain.endId];
  const hidden = chain.edges[0]?.hidden;
  const branchData =
    scope.conditionalBranchId !== null
      ? {
          conditionalNodeId: scope.parentId,
          conditionalBranchId: scope.conditionalBranchId,
        }
      : undefined;
  // The edge into a pinned finally block may be display-only; keep it as it was.
  const intoEnd = chain.edges[chain.edges.length - 1];
  return order.slice(0, -1).map((source, i) => {
    const target = order[i + 1]!;
    const template = chain.edges[i];
    const toAdder = byId.get(target)?.type === "nodeAdder";
    const base: Edge = {
      ...(target === chain.endId && intoEnd ? intoEnd : {}),
      id: template?.id ?? nanoid(),
      source,
      target,
      type: toAdder ? "default" : "edgeWithAddButton",
      style: { strokeWidth: 2 },
      ...(hidden !== undefined && { hidden }),
    } as Edge;
    if (branchData) base.data = { ...base.data, ...branchData };
    return base;
  });
}

export function isInside(nodes: AppNode[], id: string, ancestorId: string) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  for (let cur = byId.get(id); cur; cur = byId.get(cur.parentId ?? "")) {
    if (cur.id === ancestorId) return true;
    if (!cur.parentId) return false;
  }
  return false;
}

function branchContext(
  nodes: AppNode[],
  scope: DocScopeRef,
): BranchContext | undefined {
  if (scope.conditionalBranchId === null || scope.parentId === null)
    return undefined;
  const conditional = nodes.find((n) => n.id === scope.parentId);
  if (!conditional || conditional.type !== "conditional") return undefined;
  return {
    conditionalNodeId: conditional.id,
    conditionalLabel: conditional.data.label,
    branchId: scope.conditionalBranchId,
    mergeLabel: conditional.data.mergeLabel ?? null,
  };
}

function scopeHidden(nodes: AppNode[], scope: DocScopeRef): boolean {
  if (scope.parentId === null) return false;
  const parent = nodes.find((n) => n.id === scope.parentId);
  if (!parent) return false;
  if (parent.hidden) return true;
  return (
    parent.type === "conditional" &&
    scope.conditionalBranchId !== null &&
    parent.data.activeBranchId !== scope.conditionalBranchId
  );
}

export type MoveResult =
  | { kind: "ok"; graph: DocGraph }
  | { kind: "refused"; reason: string }
  | {
      kind: "forward-reference";
      movedLabel: string;
      referrerLabels: string[];
      reason: string;
    };

/** Every block in the order it runs: each chain in turn, a container before what it holds. */
export function executionOrder(
  graph: DocGraph,
  finallyId: string | null = null,
): string[] {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const out: string[] = [];
  const visit = (scope: DocScopeRef) => {
    const chain = readChain(graph, scope, finallyId);
    if (!chain) return;
    for (const id of chain.ids) {
      out.push(id);
      const node = byId.get(id);
      if (node?.type === "loop")
        visit({ parentId: id, conditionalBranchId: null });
      if (node?.type === "conditional")
        for (const branch of node.data.branches)
          visit({ parentId: id, conditionalBranchId: branch.id });
    }
  };
  visit({ parentId: null, conditionalBranchId: null });
  if (finallyId && byId.has(finallyId)) out.push(finallyId);
  return out;
}

function newForwardReferences(
  before: DocGraph,
  after: DocGraph,
  movedIds: string[],
  finallyId: string | null,
): string[] {
  const existing = new Set(
    unavailableReferences(before, movedIds, finallyId).map((v) => v.key),
  );
  return Array.from(
    new Set(
      unavailableReferences(after, movedIds, finallyId)
        .filter((v) => !existing.has(v.key))
        .map((v) => v.referrerLabel),
    ),
  );
}

// conditional id -> branch id for every branch a node sits in.
function branchPath(byId: Map<string, AppNode>, id: string) {
  const path = new Map<string, string>();
  for (let cur = byId.get(id); cur; cur = byId.get(cur.parentId ?? "")) {
    if (
      isWorkflowBlockNode(cur) &&
      cur.data.conditionalNodeId &&
      cur.data.conditionalBranchId
    )
      path.set(cur.data.conditionalNodeId, cur.data.conditionalBranchId);
    if (!cur.parentId) break;
  }
  return path;
}

// A producer inside a branch the consumer is not in (a sibling branch, or any branch when the consumer runs
// after the merge) has not run on every path to the consumer.
function onlySometimesRunsBefore(
  byId: Map<string, AppNode>,
  producer: string,
  consumer: string,
) {
  const consumerPath = branchPath(byId, consumer);
  return [...branchPath(byId, producer)].some(
    ([conditional, branch]) => consumerPath.get(conditional) !== branch,
  );
}

function unavailableReferences(
  graph: DocGraph,
  movedIds: string[],
  finallyId: string | null,
) {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const order = executionOrder(graph, finallyId);
  const blocks = graph.nodes.filter(isWorkflowBlockNode);
  const out: Array<{ key: string; referrerLabel: string }> = [];
  for (const id of movedIds) {
    const moved = byId.get(id);
    if (!moved || !isWorkflowBlockNode(moved)) continue;
    for (const other of blocks) {
      if (other.id === id) continue;
      for (const [producer, consumer] of [
        [moved, other],
        [other, moved],
      ] as const) {
        const producerIndex = order.indexOf(producer.id);
        const consumerIndex = order.indexOf(consumer.id);
        const runsLater =
          producerIndex >= 0 &&
          consumerIndex >= 0 &&
          producerIndex > consumerIndex;
        const branchOnly = onlySometimesRunsBefore(
          byId,
          producer.id,
          consumer.id,
        );
        if (!runsLater && !branchOnly) continue;
        const label = producer.data.label;
        if (
          objectContainsJinjaReference(
            consumer.data,
            label,
            SKIP_KEYS_FOR_JINJA_CHECK,
          ) ||
          objectContainsJinjaReference(
            consumer.data,
            `${label}_output`,
            SKIP_KEYS_FOR_JINJA_CHECK,
          ) ||
          namesOutputPlainly(consumer.data, `${label}_output`)
        ) {
          for (const reason of [runsLater && "order", branchOnly && "branch"])
            if (reason)
              out.push({
                key: `${reason}:${producer.id}>${consumer.id}`,
                referrerLabel: consumer.data.label,
              });
        }
      }
    }
  }
  return out;
}

/** Moves a block (with everything inside it) to `index` within `dest`. */
export function moveBlock(
  graph: DocGraph,
  id: string,
  dest: DocScopeRef,
  index: number,
  finallyId: string | null = null,
): MoveResult {
  const node = graph.nodes.find((n) => n.id === id);
  if (!node || !isWorkflowBlockNode(node))
    return { kind: "refused", reason: "That step no longer exists." };
  if (id === finallyId)
    return {
      kind: "refused",
      reason: "The step that always runs at the end stays last.",
    };
  if (dest.parentId !== null && isInside(graph.nodes, dest.parentId, id))
    return { kind: "refused", reason: "A step can't move inside itself." };
  const source = scopeOfNode(node);
  const sourceChain = readChain(graph, source, finallyId);
  const destChain = sameScope(source, dest)
    ? sourceChain
    : readChain(graph, dest, finallyId);
  if (!sourceChain || !destChain || !sourceChain.ids.includes(id))
    return {
      kind: "refused",
      reason: "This part can't be changed here. Open Visual to edit it.",
    };
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const sourceIds = sourceChain.ids.filter((x) => x !== id);
  const destIds = sameScope(source, dest) ? sourceIds : [...destChain.ids];
  const at = Math.max(0, Math.min(index, destIds.length));
  destIds.splice(at, 0, id);
  if (
    sameScope(source, dest) &&
    destIds.every((x, i) => x === sourceChain.ids[i])
  )
    return { kind: "refused", reason: "" };

  const removed = new Set(
    [...sourceChain.edges, ...destChain.edges].map((e) => e.id),
  );
  const added = sameScope(source, dest)
    ? chainEdgesFor(sourceChain, destIds, dest, byId)
    : [
        ...chainEdgesFor(sourceChain, sourceIds, source, byId),
        ...chainEdgesFor(destChain, destIds, dest, byId),
      ];
  // Template ids are reused per chain; two chains may not share one.
  const usedIds = new Set<string>();
  const edges = [
    ...graph.edges.filter((e) => !removed.has(e.id)),
    ...added.map((e) => {
      if (usedIds.has(e.id)) e = { ...e, id: nanoid() };
      usedIds.add(e.id);
      return e;
    }),
  ];

  let nodes = graph.nodes;
  if (!sameScope(source, dest)) {
    const branch = branchContext(nodes, dest);
    const moved: AppNode = {
      ...node,
      data: {
        ...node.data,
        conditionalNodeId: branch?.conditionalNodeId ?? null,
        conditionalBranchId: branch?.branchId ?? null,
        conditionalLabel: branch?.conditionalLabel ?? null,
        conditionalMergeLabel: branch?.mergeLabel ?? null,
      },
    } as AppNode;
    if (dest.parentId) moved.parentId = dest.parentId;
    else delete moved.parentId;
    // React Flow needs a parent before its children in the array.
    const subtree = new Set([id, ...descendants(nodes, id).map((n) => n.id)]);
    nodes = [
      ...nodes.filter((n) => !subtree.has(n.id)),
      ...nodes
        .filter((n) => subtree.has(n.id))
        .map((n) => (n.id === id ? moved : n)),
    ];
    nodes = updateNodeAndDescendantsVisibility(
      nodes,
      id,
      scopeHidden(nodes, dest),
    );
    // A branch keeps its loaded target while that step is still in the same conditional, so the
    // branch the step left must point at its new first step, or the merge step when it is empty.
    const left = branchContext(nodes, source);
    if (left) {
      const firstLeft = sourceIds[0] ? byId.get(sourceIds[0]) : undefined;
      const firstLabel =
        firstLeft && isWorkflowBlockNode(firstLeft)
          ? firstLeft.data.label
          : null;
      nodes = nodes.map((n) =>
        n.id === left.conditionalNodeId && n.type === "conditional"
          ? ({
              ...n,
              data: {
                ...n.data,
                branches: n.data.branches.map((b) =>
                  b.id === left.branchId
                    ? { ...b, next_block_label: firstLabel }
                    : b,
                ),
              },
            } as AppNode)
          : n,
      );
    }
  }
  const next = { nodes, edges };
  const movedIds = [id, ...descendants(graph.nodes, id).map((n) => n.id)];
  const referrerLabels = newForwardReferences(graph, next, movedIds, finallyId);
  if (referrerLabels.length > 0)
    return {
      kind: "forward-reference",
      movedLabel: node.data.label,
      referrerLabels,
      reason: `${referrerLabels.map((label) => `"${label}"`).join(", ")} would read an output that isn't available at that position.`,
    };
  return { kind: "ok", graph: next };
}

/** Where a new step goes when inserted at `index` of `scope`, in Workspace.addNode terms. */
export function insertionPoint(
  graph: DocGraph,
  scope: DocScopeRef,
  index: number,
  finallyId: string | null = null,
) {
  const chain = readChain(graph, scope, finallyId);
  if (!chain) return null;
  const at = Math.max(0, Math.min(index, chain.ids.length));
  const previous = at === 0 ? chain.startId : chain.ids[at - 1]!;
  const next = chain.ids[at] ?? chain.endId;
  const nextIsAdder =
    graph.nodes.find((n) => n.id === next)?.type === "nodeAdder";
  return {
    previous,
    next,
    parent: scope.parentId ?? undefined,
    connectingEdgeType: nextIsAdder ? "default" : "edgeWithAddButton",
    branch: branchContext(graph.nodes, scope),
  };
}

const outputKey = (label: string) => `${label}_output`;

function textValues(value: unknown, out: string[] = []): string[] {
  if (typeof value === "string") out.push(value);
  else if (Array.isArray(value)) value.forEach((v) => textValues(v, out));
  else if (value && typeof value === "object")
    Object.values(value).forEach((v) => textValues(v, out));
  return out;
}

// parameter_keys and a loop's loop_variable_reference name an output without "{{ }}".
function namesOutputPlainly(data: object, key: string): boolean {
  const { parameterKeys, loopVariableReference, loopValue } = data as {
    parameterKeys?: unknown;
    loopVariableReference?: unknown;
    loopValue?: unknown;
  };
  if (Array.isArray(parameterKeys) && parameterKeys.includes(key)) return true;
  const plain = new RegExp(
    `^\\s*${key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![a-zA-Z0-9_])`,
  );
  return [loopVariableReference, loopValue].some(
    (value) => typeof value === "string" && plain.test(value),
  );
}

// The step and every step inside it: deleting a container deletes them all.
function subtreeOutputKeys(nodes: AppNode[], id: string): string[] {
  return nodes.flatMap((n) =>
    isWorkflowBlockNode(n) && (n.id === id || isInside(nodes, n.id, id))
      ? [outputKey(n.data.label)]
      : [],
  );
}

/** Blocks outside the step whose text, settings or notes use the output of the step or of a step inside it. */
export function findOutputUsers(nodes: AppNode[], id: string): AppNode[] {
  const keys = subtreeOutputKeys(nodes, id);
  return nodes.filter(
    (n) =>
      isWorkflowBlockNode(n) &&
      n.id !== id &&
      !isInside(nodes, n.id, id) &&
      keys.some(
        (key) =>
          namesOutputPlainly(n.data, key) ||
          textValues(n.data).some((text) => containsJinjaReference(text, key)),
      ),
  );
}

/** Context inputs whose value comes from the output of the step or of a step inside it. */
export function contextInputsFedBy(
  nodes: AppNode[],
  parameters: ReadonlyArray<{
    key: string;
    parameterType: string;
    sourceParameterKey?: string | null;
  }>,
  id: string,
): string[] {
  const keys = new Set(subtreeOutputKeys(nodes, id));
  return parameters
    .filter(
      (p) =>
        p.parameterType === "context" &&
        !!p.sourceParameterKey &&
        keys.has(p.sourceParameterKey),
    )
    .map((p) => p.key);
}

function countReferences(nodes: AppNode[], key: string): number {
  const pattern = new RegExp(
    `\\{\\{\\s*${key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![a-zA-Z0-9_])`,
    "g",
  );
  return nodes.reduce(
    (count, node) =>
      count +
      (isBlock(node)
        ? textValues(node.data).reduce(
            (n, text) => n + (text.match(pattern)?.length ?? 0),
            0,
          )
        : 0),
    0,
  );
}

const LABEL = /^[A-Za-z_][A-Za-z0-9_]*$/;

export function checkLabel(
  nodes: AppNode[],
  id: string,
  label: string,
  parameterKeys: readonly string[] = [],
): string | null {
  if (!LABEL.test(label))
    return "Use letters, numbers and _, not starting with a number.";
  if (
    nodes.some(
      (n) => n.id !== id && isWorkflowBlockNode(n) && n.data.label === label,
    )
  )
    return "Another step already uses this name.";
  const outputCollision = getLabelOutputParameterKeyCollision(
    label,
    parameterKeys,
  );
  if (outputCollision)
    return `The step output "${outputCollision}" conflicts with an existing workflow parameter.`;
  return null;
}

/** A free name read from the step type: "Go to URL Block" gives go_to_url_1. */
export function newStepLabel(
  nodes: AppNode[],
  title: string,
  parameterKeys: readonly string[] = [],
): string {
  const base =
    title
      .replace(/ Block$/, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .replace(/^(?=\d)/, "step_") || "step";
  const taken = new Set(
    nodes.filter(isWorkflowBlockNode).map((n) => n.data.label),
  );
  let index = 1;
  while (
    taken.has(`${base}_${index}`) ||
    getLabelOutputParameterKeyCollision(`${base}_${index}`, parameterKeys)
  )
    index++;
  return `${base}_${index}`;
}

/** Renames a step; every `{{ old_output }}` in steps, settings and notes follows. */
export function renameStep(
  nodes: AppNode[],
  id: string,
  label: string,
): { nodes: AppNode[]; rewritten: number } {
  const node = nodes.find((n) => n.id === id);
  if (!node || !isWorkflowBlockNode(node)) return { nodes, rewritten: 0 };
  const rewritten = countReferences(nodes, outputKey(node.data.label));
  return {
    nodes: getUpdatedNodesAfterLabelUpdateForParameterKeys(
      id,
      label,
      nodes,
    ) as AppNode[],
    rewritten,
  };
}

export function renameReferencesInText(
  text: string,
  oldLabel: string,
  newLabel: string,
) {
  return replaceJinjaReference(text, outputKey(oldLabel), outputKey(newLabel));
}

function conditionalOf(graph: DocGraph, id: string) {
  const node = graph.nodes.find((n) => n.id === id);
  return node?.type === "conditional" ? node : null;
}

function withBranches(
  graph: DocGraph,
  id: string,
  branches: BranchCondition[],
  extra: Record<string, unknown> = {},
): AppNode[] {
  return graph.nodes.map((n) =>
    n.id === id
      ? ({ ...n, data: { ...n.data, branches, ...extra } } as AppNode)
      : n,
  );
}

/** Adds an empty Else-if before Otherwise. */
export function addBranch(
  graph: DocGraph,
  conditionalId: string,
  branch: BranchCondition,
): DocGraph | null {
  const conditional = conditionalOf(graph, conditionalId);
  if (!conditional) return null;
  const start = graph.nodes.find(
    (n) => n.type === "start" && n.parentId === conditionalId,
  );
  const adder = graph.nodes.find(
    (n) => n.type === "nodeAdder" && n.parentId === conditionalId,
  );
  if (!start || !adder) return null;
  const branches = conditional.data.branches;
  const next = [
    ...branches.filter((b) => !b.is_default),
    branch,
    ...branches.filter((b) => b.is_default),
  ];
  return {
    nodes: withBranches(graph, conditionalId, next),
    edges: [
      ...graph.edges,
      {
        id: `${conditionalId}-${branch.id}-start-adder`,
        type: "default",
        source: start.id,
        target: adder.id,
        style: { strokeWidth: 2 },
        data: {
          conditionalNodeId: conditionalId,
          conditionalBranchId: branch.id,
        },
        hidden: conditional.data.activeBranchId !== branch.id,
      },
    ],
  };
}

export function moveBranch(
  graph: DocGraph,
  conditionalId: string,
  branchId: string,
  delta: -1 | 1,
): DocGraph | null {
  const conditional = conditionalOf(graph, conditionalId);
  if (!conditional) return null;
  const conditions = conditional.data.branches.filter((b) => !b.is_default);
  const from = conditions.findIndex((b) => b.id === branchId);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= conditions.length) return null;
  const next = [...conditions];
  [next[from], next[to]] = [next[to]!, next[from]!];
  return {
    nodes: withBranches(graph, conditionalId, [
      ...next,
      ...conditional.data.branches.filter((b) => b.is_default),
    ]),
    edges: graph.edges,
  };
}

/** The blocks a branch removal deletes, outermost first. */
export function branchMembers(
  nodes: AppNode[],
  conditionalId: string,
  branchId: string,
) {
  return nodes.filter(
    (n) =>
      isWorkflowBlockNode(n) &&
      n.parentId === conditionalId &&
      n.data.conditionalNodeId === conditionalId &&
      n.data.conditionalBranchId === branchId,
  );
}

function removeOutputReferences(nodes: AppNode[], labels: string[]): AppNode[] {
  return labels.reduce((current, label) => {
    const key = getOutputParameterKey(label);
    return removeKeyFromNodesParameterKeys(
      removeJinjaReferenceFromNodes(current, [key]),
      key,
      label,
    );
  }, nodes);
}

export function removeBranch(
  graph: DocGraph,
  conditionalId: string,
  branchId: string,
): DocGraph | null {
  const conditional = conditionalOf(graph, conditionalId);
  if (!conditional) return null;
  const branch = conditional.data.branches.find((b) => b.id === branchId);
  const conditions = conditional.data.branches.filter((b) => !b.is_default);
  if (!branch || branch.is_default || conditions.length <= 1) return null;
  const gone = new Set<string>();
  for (const member of branchMembers(graph.nodes, conditionalId, branchId)) {
    gone.add(member.id);
    for (const inner of descendants(graph.nodes, member.id)) gone.add(inner.id);
  }
  const branches = conditional.data.branches.filter((b) => b.id !== branchId);
  const active =
    conditional.data.activeBranchId === branchId
      ? (branches[0]?.id ?? null)
      : conditional.data.activeBranchId;
  const remaining = {
    nodes: graph.nodes.filter((n) => !gone.has(n.id)),
    edges: graph.edges,
  };
  const nodes = withBranches(remaining, conditionalId, branches, {
    activeBranchId: active,
  });
  return {
    nodes: removeOutputReferences(
      nodes,
      graph.nodes.flatMap((n) =>
        gone.has(n.id) && isWorkflowBlockNode(n) ? [n.data.label] : [],
      ),
    ),
    edges: graph.edges.filter(
      (e) =>
        !gone.has(e.source) &&
        !gone.has(e.target) &&
        (e.data as { conditionalBranchId?: string } | undefined)
          ?.conditionalBranchId !== branchId,
    ),
  };
}

/** Deletes a step and everything inside it, including references to their outputs. */
export function removeBlock(
  graph: DocGraph,
  id: string,
  finallyId: string | null = null,
): DocGraph | null {
  const node = graph.nodes.find((n) => n.id === id);
  if (!node || !isWorkflowBlockNode(node)) return null;
  const scope = scopeOfNode(node);
  const chain = readChain(graph, scope, id === finallyId ? null : finallyId);
  if (!chain || !chain.ids.includes(id)) return null;
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const gone = new Set([id, ...descendants(graph.nodes, id).map((n) => n.id)]);
  const removed = new Set(chain.edges.map((e) => e.id));
  const rebuilt = chainEdgesFor(
    chain,
    chain.ids.filter((x) => x !== id),
    scope,
    byId,
  );
  const deletedLabels = graph.nodes.flatMap((n) =>
    gone.has(n.id) && isWorkflowBlockNode(n) ? [n.data.label] : [],
  );
  const remainingNodes = graph.nodes.filter((n) => !gone.has(n.id));
  const nodes = removeOutputReferences(remainingNodes, deletedLabels);
  return {
    nodes,
    edges: [
      ...graph.edges.filter(
        (e) => !removed.has(e.id) && !gone.has(e.source) && !gone.has(e.target),
      ),
      ...rebuilt,
    ],
  };
}

const KEPT_ON_TURN_INTO = [
  "label",
  "notes",
  "trailingNotes",
  "description",
  "conditionalNodeId",
  "conditionalBranchId",
  "conditionalLabel",
  "conditionalMergeLabel",
] as const;

/** Changes a step's type in place, keeping its name, notes and place in a branch. */
export function turnInto(
  graph: DocGraph,
  id: string,
  nodeType: NonNullable<WorkflowBlockNode["type"]>,
): DocGraph | null {
  const node = graph.nodes.find((n) => n.id === id);
  if (
    !node ||
    !isWorkflowBlockNode(node) ||
    node.type === nodeType ||
    // Loops and conditionals own inner nodes; Visual doesn't turn them into other steps either.
    ["loop", "conditional"].includes(node.type) ||
    ["loop", "conditional"].includes(nodeType)
  )
    return null;
  const next = createNode(
    { id, parentId: node.parentId },
    nodeType,
    node.data.label,
  ) as AppNode;
  const kept = Object.fromEntries(
    KEPT_ON_TURN_INTO.filter((key) => key in node.data).map((key) => [
      key,
      (node.data as Record<string, unknown>)[key],
    ]),
  );
  // The step's main text is what the person wrote; it moves to the new type's main text.
  const fromKey = getDocProseFields(node)[0]?.key;
  const toKey = getDocProseFields(next)[0]?.key;
  const mainText = fromKey
    ? node.data[fromKey as keyof typeof node.data]
    : null;
  if (toKey && typeof mainText === "string" && mainText.trim())
    kept[toKey] = mainText;
  return {
    nodes: graph.nodes.map((n) =>
      n.id === id
        ? ({
            ...next,
            hidden: node.hidden,
            data: { ...next.data, ...kept },
          } as AppNode)
        : n,
    ),
    edges: graph.edges,
  };
}

/** Sets one field of a step's data (text, a note). Blank notes are stored as null. */
export function setStepField(
  nodes: AppNode[],
  id: string,
  key: string,
  value: string,
): AppNode[] {
  const isNote = key === "notes" || key === "trailingNotes";
  return nodes.map((n) =>
    n.id === id
      ? ({
          ...n,
          data: { ...n.data, [key]: isNote && !value.trim() ? null : value },
        } as AppNode)
      : n,
  );
}

/** A new step's default prompt is long; the Doc's title line is that prompt, so start it empty. */
export function clearLongDefaultText(nodes: AppNode[], id: string): AppNode[] {
  const node = nodes.find((n) => n.id === id);
  if (!node || !isWorkflowBlockNode(node)) return nodes;
  const key = getDocProseFields(node)[0]?.key;
  const value = key ? (node.data as Record<string, unknown>)[key] : undefined;
  return typeof value === "string" && value.length > 60
    ? setStepField(nodes, id, key as string, "")
    : nodes;
}

const joinLines = (...parts: Array<unknown>) =>
  parts
    .filter((p): p is string => typeof p === "string" && p.trim() !== "")
    .join("\n");

const blank = (value: unknown) =>
  value === null ||
  value === undefined ||
  value === "" ||
  value === false ||
  (Array.isArray(value) && value.length === 0);

/** True when the step has settings (a URL, a body, code…) that plain text would drop. */
export function hasSettingsBeyondText(node: AppNode): boolean {
  if (!isWorkflowBlockNode(node)) return false;
  const textKeys = new Set([
    ...getDocProseFields(node).map((f) => f.key),
    "label",
    "notes",
    "trailingNotes",
    "description",
    "review",
    "showCode",
  ]);
  const defaults = createNode({ id: node.id }, node.type, node.data.label)
    .data as Record<string, unknown>;
  return Object.entries(node.data as Record<string, unknown>).some(
    ([key, value]) =>
      !textKeys.has(key) &&
      !key.startsWith("_") &&
      // Where the step sits in a conditional is not a setting.
      !key.startsWith("conditional") &&
      !blank(value) &&
      JSON.stringify(value) !== JSON.stringify(defaults[key]),
  );
}

// The backend's BLOCK_NOTE_MAX_LENGTH: a longer note fails the save.
export const NOTE_MAX_LENGTH = 2000;

export type DocNoteRef = {
  ownerNodeId: string;
  placement: "above" | "trailing";
};

/**
 * Turns a step back into plain text: its text joins the note above the next step, or the trailing note of the
 * one before. Its settings are dropped. Null when it has no neighbour to hold the note, or holds other steps.
 */
export function toNote(
  graph: DocGraph,
  id: string,
  finallyId: string | null = null,
): { graph: DocGraph; note: DocNoteRef | null } | null {
  const node = graph.nodes.find((n) => n.id === id);
  if (!node || !isWorkflowBlockNode(node)) return null;
  if (node.type === "loop" || node.type === "conditional") return null;
  const scope = scopeOfNode(node);
  const chain = readChain(graph, scope, finallyId);
  if (!chain) return null;
  const ids =
    scope.parentId === null && finallyId
      ? [...chain.ids, finallyId]
      : chain.ids;
  const at = ids.indexOf(id);
  if (at < 0) return null;
  const data = node.data as Record<string, unknown>;
  // Every line the doc shows for the step goes into the note, not just the first.
  const proseKeys = getDocProseFields(node).map((f) => f.key);
  const text = joinLines(
    data.notes,
    // The Doc shows a description too, under the text or as the main line.
    ...[...proseKeys, "description"].map((k) => data[k]),
    data.trailingNotes,
  );
  const removed = removeBlock(graph, id, finallyId);
  if (!removed) return null;
  if (!text) return { graph: removed, note: null };
  const next = ids[at + 1];
  const previous = ids[at - 1];
  const note: DocNoteRef | null = next
    ? { ownerNodeId: next, placement: "above" }
    : previous
      ? { ownerNodeId: previous, placement: "trailing" }
      : null;
  if (!note) return null;
  const key = note.placement === "above" ? "notes" : "trailingNotes";
  const nodes = removed.nodes.map((n) => {
    if (n.id !== note.ownerNodeId) return n;
    const current = (n.data as Record<string, unknown>)[key];
    const value =
      note.placement === "above"
        ? joinLines(text, current)
        : joinLines(current, text);
    return { ...n, data: { ...n.data, [key]: value } } as AppNode;
  });
  return { graph: { nodes, edges: removed.edges }, note };
}

export type DocReferenceGroup = {
  name: string;
  items: Array<{ key: string; hint: string }>;
};

const ancestorsOf = (byId: Map<string, AppNode>, id: string): AppNode[] => {
  const out: AppNode[] = [];
  for (
    let cur = byId.get(byId.get(id)?.parentId ?? "");
    cur;
    cur = byId.get(cur.parentId ?? "")
  )
    out.push(cur);
  return out;
};

/** What `@` offers at a step: the loop item when inside a loop, then inputs, then outputs of steps that run first. */
export function referencesAt(
  graph: DocGraph,
  id: string,
  inputKeys: string[],
  finallyId: string | null = null,
  includeStepOutputs = true,
): DocReferenceGroup[] {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const ancestors = ancestorsOf(byId, id);
  const groups: DocReferenceGroup[] = [];
  if (ancestors.some((a) => a.type === "loop"))
    groups.push({
      name: "This loop",
      items: LOOP_RESERVED_PARAMETERS.map((p) => ({
        key: p.key,
        hint: p.description,
      })),
    });
  if (inputKeys.length)
    groups.push({
      name: "Inputs",
      items: inputKeys.map((key) => ({ key, hint: "Input" })),
    });
  if (!includeStepOutputs) return groups;
  const order = executionOrder(graph, finallyId);
  const at = order.indexOf(id);
  const ancestorIds = new Set(ancestors.map((a) => a.id));
  const earlier = (at < 0 ? [] : order.slice(0, at)).filter(
    (other) =>
      !ancestorIds.has(other) && !onlySometimesRunsBefore(byId, other, id),
  );
  const outputs = earlier.flatMap((other) => {
    const node = byId.get(other);
    return node && isWorkflowBlockNode(node)
      ? [{ key: outputKey(node.data.label), hint: "Saved by an earlier step" }]
      : [];
  });
  if (outputs.length) groups.push({ name: "Earlier steps", items: outputs });
  return groups;
}
