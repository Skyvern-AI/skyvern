// The collapse chevron reshuffles canvas layout, so it follows the same
// freezes as the other canvas mutations (read-only, canvas lock).
export type CollapseModeGateInputs = {
  isReadOnlyScope: boolean;
  isCanvasLocked: boolean;
};

export function isCollapseGated({
  isReadOnlyScope,
  isCanvasLocked,
}: CollapseModeGateInputs): boolean {
  return isReadOnlyScope || isCanvasLocked;
}
