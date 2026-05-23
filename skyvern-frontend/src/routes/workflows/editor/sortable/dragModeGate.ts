/**
 * Drag mode gate.
 *
 * Recording mode captures live browser events against the workflow graph,
 * and a reorder committed mid-record would shift block identities under the
 * recorder. Canvas lock (the Controls panel padlock) flips React Flow's
 * nodesDraggable/Connectable/Selectable off; drag-to-reorder should follow.
 * Debug mode (build view) used to gate drag too, but collapse and drag are
 * pure visual / structural toggles that don't change run state, so they
 * stay interactive on /build.
 *
 * `NodeHeader` calls {@link isDragGatedByMode} to decide whether the grip
 * handle should be interactive, and {@link getDragGateReason} to surface a
 * tooltip that tells the user how to re-enable dragging.
 */

export type DragModeGateInputs = {
  isRecording: boolean;
  isCanvasLocked: boolean;
};

export function isDragGatedByMode({
  isRecording,
  isCanvasLocked,
}: DragModeGateInputs): boolean {
  return isRecording || isCanvasLocked;
}

export function getDragGateReason({
  isRecording,
  isCanvasLocked,
}: DragModeGateInputs): string | null {
  if (isRecording) return "Stop recording to reorder blocks";
  if (isCanvasLocked) return "Unlock canvas to reorder blocks";
  return null;
}
