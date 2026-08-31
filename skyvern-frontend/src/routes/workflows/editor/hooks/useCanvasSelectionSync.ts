import { useStoreApi } from "@xyflow/react";
import { useEffect } from "react";

/**
 * Keeps React Flow's internal `selected` flag in lockstep with the app's
 * selected-block store; downstream RF features (delete-key, multi-select)
 * read it.
 *
 * The write goes through React Flow's own selection actions — the ones its
 * canvas click path uses — so it reaches `onNodesChange` as a `select`
 * change. Writing it with `setNodes` instead emits `replace`, which is
 * indistinguishable from a real edit there and marks the workflow dirty on
 * selection alone (timeline jump, block search, deep link).
 */
export function useCanvasSelectionSync(selectedBlockId: string | null): void {
  const store = useStoreApi();

  useEffect(() => {
    const { addSelectedNodes, unselectNodesAndEdges } = store.getState();
    // Clear first: while a multi-select modifier is held `addSelectedNodes`
    // only adds, which would leave earlier blocks selected on the canvas
    // alongside the sidebar's single selection.
    unselectNodesAndEdges();
    if (selectedBlockId !== null) {
      addSelectedNodes([selectedBlockId]);
    }
  }, [selectedBlockId, store]);
}
