import type { NodeChange } from "@xyflow/react";

import { isWorkflowMutation } from "@/store/WorkflowYamlEditorStore";

import type { AppNode } from "./nodes";

// Node data the canvas measures off the DOM for layout. It never reaches the
// save payload, so a write that only moves one of these is not an edit: the
// studio hides the editor pane with display:none, which re-measures every
// loop/conditional header and would otherwise mark an untouched workflow
// unsaved (SKY-13875).
const LAYOUT_MEASUREMENT_DATA_KEYS = new Set(["_headerHeight"]);

type UnknownRecord = Record<string, unknown>;

function onlyLayoutMeasurementsDiffer(
  previous: UnknownRecord,
  next: UnknownRecord,
): boolean {
  let measurementChanged = false;
  for (const key of new Set([...Object.keys(previous), ...Object.keys(next)])) {
    if (previous[key] === next[key]) {
      continue;
    }
    if (!LAYOUT_MEASUREMENT_DATA_KEYS.has(key)) {
      return false;
    }
    measurementChanged = true;
  }
  return measurementChanged;
}

// `updateNodeData` reaches `onNodesChange` as a whole-node `replace`, the same
// shape a real edit arrives in, so the two are told apart by diffing against
// the node the change replaces. Anything that cannot be matched or compared by
// identity counts as an edit, so a `previousNodes` lagging the change only ever
// errs toward marking the workflow unsaved.
function isLayoutMeasurementReplace(
  change: NodeChange<AppNode>,
  previousNodes: Array<AppNode>,
): boolean {
  if (change.type !== "replace") {
    return false;
  }
  const previous = previousNodes.find((node) => node.id === change.id);
  if (!previous) {
    return false;
  }
  const previousNode = previous as unknown as UnknownRecord;
  const nextNode = change.item as unknown as UnknownRecord;
  const previousKeys = Object.keys(previousNode);
  if (previousKeys.length !== Object.keys(nextNode).length) {
    return false;
  }
  for (const key of previousKeys) {
    if (key !== "data" && previousNode[key] !== nextNode[key]) {
      return false;
    }
  }
  return onlyLayoutMeasurementsDiffer(
    previous.data as unknown as UnknownRecord,
    change.item.data as unknown as UnknownRecord,
  );
}

export function changesIncludeWorkflowEdit(
  changes: Array<NodeChange<AppNode>>,
  previousNodes: Array<AppNode>,
): boolean {
  return changes.some(
    (change) =>
      isWorkflowMutation(change) &&
      !isLayoutMeasurementReplace(change, previousNodes),
  );
}
