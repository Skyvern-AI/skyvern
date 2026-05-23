import { collapsibleRfNodeTypes } from "./collapsibleBlockTypes";
import { isBlockCollapsedAt } from "./useNodeCollapseStore";
import { descendants } from "../workflowEditorUtils";
import type { AppNode } from "../nodes";

// `node.data` is a discriminated union across 26 block types but every
// variant exposes `label?: string`. Centralising the cast here keeps the
// `as { ... }` shape escape in one place; if the data shape ever drifts
// it fails in this helper instead of scattering across call sites.
function getNodeLabel(node: AppNode): string | undefined {
  const data = node.data as { label?: unknown };
  return typeof data?.label === "string" ? data.label : undefined;
}

function getActiveBranchId(node: AppNode): string | null | undefined {
  return (node.data as { activeBranchId?: string | null }).activeBranchId;
}

function getConditionalBranchId(node: AppNode): string | null | undefined {
  return (node.data as { conditionalBranchId?: string | null })
    .conditionalBranchId;
}

// Computes effective `hidden` for every descendant of `rootId` based on
// three signals (top-down so each decision can read its parent's resolved
// state):
//   1. `isRootCollapsed` — when the root (loop or conditional) is being
//      collapsed, every descendant is hidden in a single pass.
//   2. Inner collapsed ancestor — when the root is being expanded, a
//      descendant whose ancestor (between this node and rootId, exclusive
//      of rootId) is in `collapsedSet` stays hidden.
//   3. Inactive conditional branch — a node whose direct parent is a
//      conditional and whose `data.conditionalBranchId` does not match the
//      conditional's `data.activeBranchId` is hidden.
// Cascades: if a parent resolves to hidden, this node is hidden too.
//
// Nodes outside the rootId subtree are returned unchanged.
export function applyDescendantCollapseVisibility(
  nodes: Array<AppNode>,
  rootId: string,
  isRootCollapsed: boolean,
  isLabelCollapsed: (label: string) => boolean,
): Array<AppNode> {
  const descs = descendants(nodes, rootId);
  if (descs.length === 0) return nodes;

  const descIds = new Set(descs.map((d) => d.id));

  if (isRootCollapsed) {
    return nodes.map((n) => (descIds.has(n.id) ? { ...n, hidden: true } : n));
  }

  const indexById = new Map(nodes.map((node, i) => [node.id, i]));
  const result = [...nodes];
  // descendants() returns nodes parent-first, so any descendant whose parent
  // is itself a descendant has already been written to `resolved` by the time
  // we read it here. See descendants() in workflowEditorUtils.ts.
  const resolved = new Map<string, boolean>();

  for (const desc of descs) {
    const idx = indexById.get(desc.id);
    if (idx === undefined) continue;

    const node = result[idx]!;
    const parentIdx = node.parentId ? indexById.get(node.parentId) : undefined;
    const parent = parentIdx !== undefined ? result[parentIdx] : undefined;

    let shouldHide = false;

    if (parent) {
      if (parent.id !== rootId && resolved.get(parent.id) === true) {
        shouldHide = true;
      }

      if (!shouldHide && parent.id !== rootId) {
        const parentLabel = getNodeLabel(parent);
        if (parentLabel && isLabelCollapsed(parentLabel)) {
          shouldHide = true;
        }
      }

      if (!shouldHide && parent.type === "conditional") {
        const activeBranchId = getActiveBranchId(parent);
        const myBranchId = getConditionalBranchId(node);
        if (myBranchId !== null && myBranchId !== undefined) {
          if (myBranchId !== activeBranchId) {
            shouldHide = true;
          }
        }
      }
    }

    resolved.set(desc.id, shouldHide);
    result[idx] = { ...node, hidden: shouldHide };
  }

  return result;
}

// Replays every persisted collapse for `workflowId` against `nodes`, hiding
// each collapsed block's descendants. Use after callers replace the React
// Flow nodes wholesale (workflow load, history version select, etc.) so the
// per-conditional `useEffect` cascades — which only run on isCollapsed
// flips — don't get silently undone by `getElements()` resetting `hidden`.
export function replayPersistedCollapseVisibility(
  nodes: Array<AppNode>,
  workflowId: string,
  collapsedSet: Record<string, boolean>,
): Array<AppNode> {
  const isLabelCollapsed = (label: string): boolean =>
    isBlockCollapsedAt(collapsedSet, workflowId, label);

  let result = nodes;
  for (const node of nodes) {
    if (!node.type || !collapsibleRfNodeTypes.has(node.type)) continue;
    const label = getNodeLabel(node);
    if (!label) continue;
    if (!isLabelCollapsed(label)) continue;
    result = applyDescendantCollapseVisibility(
      result,
      node.id,
      true,
      isLabelCollapsed,
    );
  }
  return result;
}
