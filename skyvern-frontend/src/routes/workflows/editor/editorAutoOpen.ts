// One-shot arm/fire decision for auto-opening the Editor pane when Copilot's
// first build or mid-turn draft lands blocks on a previously-empty agent.
// Snap-backs never fire it, and once it fires it stays disarmed for the rest
// of the state's lifetime.
export type EditorAutoOpenState = {
  armed: boolean;
};

export function initialEditorAutoOpenState(
  blockCount: number,
): EditorAutoOpenState {
  return { armed: blockCount === 0 };
}

export function shouldAutoOpenEditor(
  state: EditorAutoOpenState,
  update: {
    embedded: boolean;
    applied: boolean | undefined;
    midTurnDraft?: boolean;
    blockCount: number;
  },
): { fire: boolean; nextState: EditorAutoOpenState } {
  const fire =
    state.armed &&
    update.embedded &&
    Boolean(update.applied || update.midTurnDraft) &&
    update.blockCount > 0;
  return { fire, nextState: fire ? { armed: false } : state };
}
