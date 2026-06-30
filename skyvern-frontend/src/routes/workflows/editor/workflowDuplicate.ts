import type { Edge } from "@xyflow/react";

import type { BranchContext } from "@/store/WorkflowPanelStore";

import { replaceJinjaReference } from "./jinjaReferences";
import type { AppNode, WorkflowBlockNode } from "./nodes";
import type { NodeBaseData } from "./nodes/types";
import { shouldKeepExistingEdgeForInsertion } from "./workflowInsertion";

type GenerateId = () => string;
type GenerateLabel = (existingLabels: Array<string>) => string;

type DuplicateBlockBelowOptions = {
  nodes: Array<AppNode>;
  edges: Array<Edge>;
  nodeId: string;
  generateId: GenerateId;
  generateLabel: GenerateLabel;
};

type DuplicateBlockBelowResult = {
  nodes: Array<AppNode>;
  edges: Array<Edge>;
  duplicatedNodeId: string;
  duplicatedLabel: string;
  position: number;
};

type BranchConditionLike = {
  id: string;
};

type ConditionalDataLike = NodeBaseData & {
  activeBranchId: string | null;
  branches: Array<BranchConditionLike>;
  mergeLabel?: string | null;
};

const SKIP_KEYS_FOR_REFERENCE_REWRITE = new Set([
  "id",
  "key",
  "label",
  "nodeId",
  "parameterKeys",
  "type",
]);

function clonePlain<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function isWorkflowBlockNodeLike(node: AppNode): node is WorkflowBlockNode {
  return node.type !== "nodeAdder" && node.type !== "start";
}

function outputKey(label: string): string {
  return `${label}_output`;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function branchKey(conditionalNodeId: string, branchId: string): string {
  return `${conditionalNodeId}:${branchId}`;
}

function hasAncestor(
  nodesById: Map<string, AppNode>,
  node: AppNode,
  ancestorId: string,
): boolean {
  let currentParentId = node.parentId;
  const visited = new Set<string>();

  while (currentParentId && !visited.has(currentParentId)) {
    if (currentParentId === ancestorId) {
      return true;
    }
    visited.add(currentParentId);
    currentParentId = nodesById.get(currentParentId)?.parentId;
  }

  return false;
}

function rewriteStringReferences(
  value: string,
  outputKeyMap: Map<string, string>,
): string {
  let next = value;
  outputKeyMap.forEach((newKey, oldKey) => {
    next = replaceJinjaReference(next, oldKey, newKey);
    // Code blocks receive upstream outputs as plain identifiers, while some
    // serialized config stores the same output keys inside quoted strings.
    next = next.replace(
      new RegExp(
        `(^|[^A-Za-z0-9_])${escapeRegExp(oldKey)}(?=$|[^A-Za-z0-9_])`,
        "g",
      ),
      (_match, prefix: string) => `${prefix}${newKey}`,
    );
  });
  return next;
}

function rewriteReferencesInValue<T>(
  value: T,
  outputKeyMap: Map<string, string>,
): T {
  if (typeof value === "string") {
    return rewriteStringReferences(value, outputKeyMap) as T;
  }

  if (value === null || value === undefined) {
    return value;
  }

  if (Array.isArray(value)) {
    return value.map((item) =>
      rewriteReferencesInValue(item, outputKeyMap),
    ) as T;
  }

  if (typeof value !== "object") {
    return value;
  }

  const result: Record<string, unknown> = {};
  Object.entries(value).forEach(([entryKey, entryValue]) => {
    result[entryKey] = SKIP_KEYS_FOR_REFERENCE_REWRITE.has(entryKey)
      ? entryValue
      : rewriteReferencesInValue(entryValue, outputKeyMap);
  });

  return result as T;
}

function rewriteOutputReferencesInData<T extends NodeBaseData>(
  data: T,
  outputKeyMap: Map<string, string>,
): T {
  const rewritten = rewriteReferencesInValue(data, outputKeyMap) as T;
  const mutable = rewritten as T & {
    loopVariableReference?: string;
    parameterKeys?: Array<string> | null;
  };

  if (Array.isArray(mutable.parameterKeys)) {
    mutable.parameterKeys = mutable.parameterKeys.map(
      (key) => outputKeyMap.get(key) ?? key,
    );
  }

  if (mutable.loopVariableReference) {
    mutable.loopVariableReference =
      outputKeyMap.get(mutable.loopVariableReference) ??
      mutable.loopVariableReference;
  }

  return mutable;
}

function isConditionalData(data: NodeBaseData): data is ConditionalDataLike {
  const candidate = data as Partial<ConditionalDataLike>;
  return Array.isArray(candidate.branches);
}

function getBranchContextForNode(
  node: WorkflowBlockNode,
): BranchContext | undefined {
  const data = node.data;
  if (!data.conditionalNodeId || !data.conditionalBranchId) {
    return undefined;
  }

  return {
    conditionalNodeId: data.conditionalNodeId,
    conditionalLabel: data.conditionalLabel ?? data.conditionalNodeId,
    branchId: data.conditionalBranchId,
    mergeLabel: data.conditionalMergeLabel ?? null,
  };
}

function cloneEdgeData(
  data: Edge["data"],
  idMap: Map<string, string>,
  branchIdMap: Map<string, string>,
): Edge["data"] {
  if (!data || typeof data !== "object") {
    return data;
  }

  const cloned = clonePlain(data) as Record<string, unknown>;
  const conditionalNodeId = cloned.conditionalNodeId;
  const conditionalBranchId = cloned.conditionalBranchId;

  if (typeof conditionalNodeId === "string" && idMap.has(conditionalNodeId)) {
    cloned.conditionalNodeId = idMap.get(conditionalNodeId);

    if (typeof conditionalBranchId === "string") {
      cloned.conditionalBranchId =
        branchIdMap.get(branchKey(conditionalNodeId, conditionalBranchId)) ??
        conditionalBranchId;
    }
  }

  return cloned;
}

function makeEdge({
  branch,
  generateId,
  source,
  target,
  type,
}: {
  branch?: BranchContext;
  generateId: GenerateId;
  source: string;
  target: string;
  type: string;
}): Edge {
  return {
    id: generateId(),
    type,
    source,
    target,
    style: { strokeWidth: 2 },
    data: branch
      ? {
          conditionalBranchId: branch.branchId,
          conditionalNodeId: branch.conditionalNodeId,
        }
      : undefined,
  };
}

function findInsertionEdge(
  node: WorkflowBlockNode,
  edges: Array<Edge>,
  subtreeIds: Set<string>,
): Edge | undefined {
  const outgoingEdges = edges.filter((edge) => edge.source === node.id);
  if (outgoingEdges.length === 0) {
    return undefined;
  }

  const branch = getBranchContextForNode(node);
  if (branch) {
    const branchEdge = outgoingEdges.find((edge) => {
      const edgeData = edge.data as
        | { conditionalBranchId?: string; conditionalNodeId?: string }
        | undefined;
      return (
        edgeData?.conditionalBranchId === branch.branchId &&
        edgeData?.conditionalNodeId === branch.conditionalNodeId
      );
    });
    if (branchEdge) {
      return branchEdge;
    }
  }

  return (
    outgoingEdges.find((edge) => !subtreeIds.has(edge.target)) ??
    outgoingEdges[0]
  );
}

export function duplicateBlockBelow({
  nodes,
  edges,
  nodeId,
  generateId,
  generateLabel,
}: DuplicateBlockBelowOptions): DuplicateBlockBelowResult | null {
  const sourceNode = nodes.find((node) => node.id === nodeId);
  if (!sourceNode || !isWorkflowBlockNodeLike(sourceNode)) {
    return null;
  }

  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const subtreeNodes = nodes.filter(
    (node) => node.id === nodeId || hasAncestor(nodesById, node, nodeId),
  );
  const subtreeIds = new Set(subtreeNodes.map((node) => node.id));
  const idMap = new Map<string, string>();
  subtreeNodes.forEach((node) => {
    idMap.set(node.id, generateId());
  });

  const existingLabels = nodes
    .filter(isWorkflowBlockNodeLike)
    .map((node) => node.data.label);
  const labelMap = new Map<string, string>();
  subtreeNodes.filter(isWorkflowBlockNodeLike).forEach((node) => {
    const newLabel = generateLabel(existingLabels);
    existingLabels.push(newLabel);
    labelMap.set(node.data.label, newLabel);
  });

  const outputKeyMap = new Map<string, string>();
  labelMap.forEach((newLabel, oldLabel) => {
    outputKeyMap.set(outputKey(oldLabel), outputKey(newLabel));
  });

  const branchIdMap = new Map<string, string>();
  subtreeNodes.filter(isWorkflowBlockNodeLike).forEach((node) => {
    if (!isConditionalData(node.data)) {
      return;
    }
    node.data.branches.forEach((branch) => {
      branchIdMap.set(branchKey(node.id, branch.id), generateId());
    });
  });

  const clonedNodes = subtreeNodes.map((node): AppNode => {
    const cloned = clonePlain(node);
    cloned.id = idMap.get(node.id)!;
    cloned.position = { x: 0, y: 0 };
    cloned.selected = false;

    if (node.parentId) {
      cloned.parentId = idMap.get(node.parentId) ?? node.parentId;
    }

    if (!isWorkflowBlockNodeLike(node) || !isWorkflowBlockNodeLike(cloned)) {
      return cloned;
    }

    const clonedData = rewriteOutputReferencesInData(
      clonePlain(node.data),
      outputKeyMap,
    );

    clonedData.label = labelMap.get(node.data.label)!;

    if (
      clonedData.conditionalNodeId &&
      idMap.has(clonedData.conditionalNodeId)
    ) {
      const oldConditionalNodeId = clonedData.conditionalNodeId;
      const oldConditionalNode = nodesById.get(oldConditionalNodeId);
      clonedData.conditionalNodeId = idMap.get(oldConditionalNodeId)!;
      if (oldConditionalNode && isWorkflowBlockNodeLike(oldConditionalNode)) {
        clonedData.conditionalLabel =
          labelMap.get(oldConditionalNode.data.label) ??
          clonedData.conditionalLabel;
      }
      if (clonedData.conditionalBranchId) {
        clonedData.conditionalBranchId =
          branchIdMap.get(
            branchKey(oldConditionalNodeId, clonedData.conditionalBranchId),
          ) ?? clonedData.conditionalBranchId;
      }
    }

    if (
      clonedData.conditionalMergeLabel &&
      labelMap.has(clonedData.conditionalMergeLabel)
    ) {
      clonedData.conditionalMergeLabel = labelMap.get(
        clonedData.conditionalMergeLabel,
      )!;
    }

    if (isConditionalData(clonedData)) {
      const oldActiveBranchId = clonedData.activeBranchId;
      clonedData.branches = clonedData.branches.map((branch) => ({
        ...branch,
        id: branchIdMap.get(branchKey(node.id, branch.id)) ?? branch.id,
      }));
      clonedData.activeBranchId = oldActiveBranchId
        ? (branchIdMap.get(branchKey(node.id, oldActiveBranchId)) ??
          oldActiveBranchId)
        : oldActiveBranchId;
      if (clonedData.mergeLabel && labelMap.has(clonedData.mergeLabel)) {
        clonedData.mergeLabel = labelMap.get(clonedData.mergeLabel)!;
      }
    }

    cloned.data = clonedData;
    return cloned;
  });

  const internalEdges = edges
    .filter(
      (edge) => subtreeIds.has(edge.source) && subtreeIds.has(edge.target),
    )
    .map((edge): Edge => {
      return {
        ...clonePlain(edge),
        id: generateId(),
        source: idMap.get(edge.source)!,
        target: idMap.get(edge.target)!,
        data: cloneEdgeData(edge.data, idMap, branchIdMap),
      };
    });

  const insertionEdge = findInsertionEdge(sourceNode, edges, subtreeIds);
  const next = insertionEdge?.target ?? null;
  const branch = getBranchContextForNode(sourceNode);
  const editedEdges = edges.filter((edge) =>
    shouldKeepExistingEdgeForInsertion(edge, {
      branch,
      next,
      previous: sourceNode.id,
    }),
  );

  const duplicatedNodeId = idMap.get(sourceNode.id)!;
  const insertionEdges = [
    makeEdge({
      branch,
      generateId,
      source: sourceNode.id,
      target: duplicatedNodeId,
      type: "edgeWithAddButton",
    }),
  ];

  if (next) {
    insertionEdges.push(
      makeEdge({
        branch,
        generateId,
        source: duplicatedNodeId,
        target: next,
        type: insertionEdge?.type ?? "default",
      }),
    );
  }

  const sourceSubtreeIndexes = subtreeNodes.map((node) => nodes.indexOf(node));
  const insertIndex = Math.max(...sourceSubtreeIndexes) + 1;
  const nextNodes = [
    ...nodes.slice(0, insertIndex),
    ...clonedNodes,
    ...nodes.slice(insertIndex),
  ];

  return {
    nodes: nextNodes,
    edges: [
      ...editedEdges.filter((edge) => edge.source !== sourceNode.id),
      // Keep the inserted bridge as the first source-owned outgoing edge;
      // branch-specific source edges are preserved immediately after it.
      ...insertionEdges,
      ...editedEdges.filter((edge) => edge.source === sourceNode.id),
      ...internalEdges,
    ],
    duplicatedNodeId,
    duplicatedLabel: labelMap.get(sourceNode.data.label)!,
    position: insertIndex,
  };
}
