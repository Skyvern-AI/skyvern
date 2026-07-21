// The collapse chevron reshuffles canvas layout, so it follows the same
// freezes as the other canvas mutations (recording, read-only, canvas lock).
export type CollapseModeGateInputs = {
  isRecording: boolean;
  isReadOnlyScope: boolean;
  isCanvasLocked: boolean;
};

export function isCollapseGated({
  isRecording,
  isReadOnlyScope,
  isCanvasLocked,
}: CollapseModeGateInputs): boolean {
  return isRecording || isReadOnlyScope || isCanvasLocked;
}
