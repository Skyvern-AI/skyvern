import { useStore } from "@xyflow/react";

// Canvas is "locked" when ToggleInteractivityControl has cleared all three
// React Flow interactivity flags. Mirrors the selector used by the toggle so
// downstream gates (drag, delete) see the same truth.
const interactiveSelector = (s: {
  nodesDraggable: boolean;
  nodesConnectable: boolean;
  elementsSelectable: boolean;
}) => s.nodesDraggable || s.nodesConnectable || s.elementsSelectable;

export function useIsCanvasLocked(): boolean {
  return !useStore(interactiveSelector);
}
