import type { Node, NodeChange } from "@xyflow/react";

/**
 * Whether a batch of React Flow node changes contains a workflow edit.
 *
 * `select` and `dimensions` changes are canvas state, not workflow data, so
 * they are excluded. `position` counts only at `dragging === false`, the end
 * of a real drag gesture: programmatic position updates (mount-time layout,
 * setNodes from node components) leave `dragging` undefined.
 */
export function hasStructuralNodeChange<NodeType extends Node>(
  changes: Array<NodeChange<NodeType>>,
): boolean {
  return changes.some(
    (change) =>
      change.type === "add" ||
      change.type === "remove" ||
      change.type === "replace" ||
      (change.type === "position" && change.dragging === false),
  );
}
