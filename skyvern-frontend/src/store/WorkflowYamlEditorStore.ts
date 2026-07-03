import { create } from "zustand";

type WorkflowYamlEditorState = {
  active: boolean;
  draft: string;
  // YAML captured when the editor opened. Used to short-circuit the commit when
  // nothing was edited (the graph already matches, so no reparse is needed).
  entrySnapshot: string;
  error: string | null;
  committing: boolean;
  // Reparses the draft into the graph and closes on success; returns false on
  // invalid YAML so callers abort. With persist=true it also saves the draft
  // (for the top-bar/nav "Save" paths). Survives close() — registered on mount.
  commit: ((persist?: boolean) => Promise<boolean>) | null;
  open: (yaml: string) => void;
  setDraft: (yaml: string) => void;
  setError: (error: string | null) => void;
  setCommitting: (committing: boolean) => void;
  registerCommit: (
    commit: ((persist?: boolean) => Promise<boolean>) | null,
  ) => void;
  // Serializes the live canvas and opens the editor. Registered by the
  // embedded Workspace so shell chrome (the Editor pane header's toggle) can
  // enter Code mode without owning the serialization.
  enterYamlMode: (() => void) | null;
  registerEnterYamlMode: (enter: (() => void) | null) => void;
  close: () => void;
};

export const useWorkflowYamlEditorStore = create<WorkflowYamlEditorState>(
  (set) => ({
    active: false,
    draft: "",
    entrySnapshot: "",
    error: null,
    committing: false,
    commit: null,
    open: (yaml) =>
      set({
        active: true,
        draft: yaml,
        entrySnapshot: yaml,
        error: null,
        committing: false,
      }),
    setDraft: (yaml) => set({ draft: yaml, error: null }),
    setError: (error) => set({ error }),
    setCommitting: (committing) => set({ committing }),
    registerCommit: (commit) => set({ commit }),
    enterYamlMode: null,
    registerEnterYamlMode: (enterYamlMode) => set({ enterYamlMode }),
    close: () =>
      set({
        active: false,
        draft: "",
        entrySnapshot: "",
        error: null,
        committing: false,
      }),
  }),
);

export function isWorkflowYamlDirty(state: {
  draft: string;
  entrySnapshot: string;
}): boolean {
  return state.draft !== state.entrySnapshot;
}

// Shared entry point for the commit-on-switch flow used by the overlay's Visual
// toggle, the top-bar save, and the nav-blocker "Save changes" dialog. Guards
// against a re-entrant commit and toggles the committing flag around it.
// Returns false when a commit is already running, none is registered, or the
// draft is invalid.
export async function commitYamlDraft(persist: boolean): Promise<boolean> {
  const store = useWorkflowYamlEditorStore.getState();
  if (store.committing || !store.commit) {
    return false;
  }
  store.setCommitting(true);
  try {
    return await store.commit(persist);
  } finally {
    store.setCommitting(false);
  }
}
