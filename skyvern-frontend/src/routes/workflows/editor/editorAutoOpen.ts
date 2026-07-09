// One-shot arm/fire decision for auto-opening the Editor pane when Copilot's
// first applied build lands blocks on a previously-empty agent (SKY-11763).
// Drafts/snap-backs (`applied` false/undefined) never fire it, and once it
// fires it stays disarmed for the rest of the state's lifetime — closing the
// pane afterward has no bearing since this state has no notion of pane-open.
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
    blockCount: number;
  },
): { fire: boolean; nextState: EditorAutoOpenState } {
  const fire =
    state.armed &&
    update.embedded &&
    Boolean(update.applied) &&
    update.blockCount > 0;
  return { fire, nextState: fire ? { armed: false } : state };
}
