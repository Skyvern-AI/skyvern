import type { CopilotAcceptSnapshot } from "@/routes/workflows/editor/editorStateSnapshot";
import { create } from "zustand";
import { toast } from "@/components/ui/use-toast";
import { useRecordedBlocksStore } from "./RecordedBlocksStore";
import { useRecordingStore } from "./useRecordingStore";

export const SAVE_STALE_MESSAGE =
  "Saved on the server, but local edits changed during the save; reload.";

export type YamlCommitOwner = {
  workflowPermanentId: string;
  active: boolean;
};

export type YamlCommitContext = { owner: YamlCommitOwner; revision: number };

type WorkflowYamlEditorState = {
  active: boolean;
  draft: string;
  // YAML captured when the editor opened. Used to short-circuit the commit when
  // nothing was edited (the graph already matches, so no reparse is needed).
  entrySnapshot: string;
  stale: boolean;
  error: string | null;
  committing: boolean;
  commitInProgress: boolean;
  authoringInProgress: boolean;
  authoringAction: symbol | null;
  authoringActionOwner: YamlCommitOwner | null;
  lockKind: "yaml" | "save" | "copilot" | null;
  commitOwner: YamlCommitOwner | null;
  persistingOwner: YamlCommitOwner | null;
  // Pending writes outlive their editor; the scalar lock fields describe the active workflow.
  pendingSaves: Record<
    string,
    {
      owner: YamlCommitOwner;
      kind: "yaml" | "save";
      persisting: boolean;
      reloadProtectionUnavailable?: boolean;
      slow?: boolean;
      restored?: StoredSave;
    }
  >;
  pendingAccepts: Record<
    string,
    { reservation: symbol; snapshot: CopilotAcceptSnapshot }
  >;
  editorOwner: YamlCommitOwner | null;
  // Reserved by each send and manual acceptance through terminal/recovery handling.
  copilotAcceptance: symbol | null;
  applyingCopilotOwner: symbol | undefined;
  revision: number;
  bumpRevision: () => void;
  setCommitInProgress: (value: boolean) => void;
  // Reparses the draft into the graph and closes on success; returns false on
  // invalid YAML so callers abort. With persist=true it also saves the draft
  // (for the top-bar/nav "Save" paths). Survives close() — registered on mount.
  commit:
    | ((
        persist?: boolean,
        codeCacheDeletionApproved?: boolean,
      ) => Promise<boolean>)
    | null;
  flushDraft: (() => void) | null;
  open: (yaml: string) => void;
  setDraft: (yaml: string) => void;
  setError: (error: string | null) => void;
  setCommitting: (committing: boolean) => void;
  registerCommit: (
    commit:
      | ((
          persist?: boolean,
          codeCacheDeletionApproved?: boolean,
        ) => Promise<boolean>)
      | null,
  ) => void;
  // Serializes the live canvas and opens the editor. Registered by Workspace
  // so header chrome outside its closure (studio's Editor pane header, the
  // legacy overflow menu) can enter Code mode without owning the serialization.
  enterYamlMode: (() => void) | null;
  registerEnterYamlMode: (enter: (() => void) | null) => void;
  close: () => void;
};

export function selectEditorMutationLocked(
  state: WorkflowYamlEditorState,
): boolean {
  return state.commitInProgress || state.copilotAcceptance !== null;
}

export function isEditorMutationLocked(): boolean {
  return selectEditorMutationLocked(useWorkflowYamlEditorStore.getState());
}

export const useWorkflowYamlEditorStore = create<WorkflowYamlEditorState>(
  (set) => ({
    active: false,
    draft: "",
    entrySnapshot: "",
    stale: false,
    error: null,
    committing: false,
    commitInProgress: false,
    authoringInProgress: false,
    authoringAction: null,
    authoringActionOwner: null,
    lockKind: null,
    commitOwner: null,
    persistingOwner: null,
    pendingAccepts: {},
    pendingSaves: {},
    editorOwner: null,
    copilotAcceptance: null,
    applyingCopilotOwner: undefined,
    revision: 0,
    bumpRevision: () => set((state) => ({ revision: state.revision + 1 })),
    setCommitInProgress: (commitInProgress) =>
      set((state) =>
        state.lockKind === "save"
          ? state
          : {
              commitInProgress,
              committing: commitInProgress,
              lockKind: commitInProgress
                ? "yaml"
                : state.copilotAcceptance
                  ? "copilot"
                  : null,
            },
      ),
    commit: null,
    flushDraft: null,
    open: (yaml) =>
      set({
        active: true,
        draft: yaml,
        entrySnapshot: yaml,
        stale: false,
        error: null,
        committing: false,
      }),
    setDraft: (yaml) => {
      if (refuseMutationDuringYamlCommit()) return;
      set((state) => ({
        draft: yaml,
        error: state.stale ? staleYamlMessage : null,
      }));
    },
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
        stale: false,
        error: null,
        committing: false,
      }),
  }),
);

function syncAuthoringInProgress(): void {
  const state = useWorkflowYamlEditorStore.getState();
  const recording = useRecordingStore.getState();
  const recorded = useRecordedBlocksStore.getState();
  const owner = state.editorOwner;
  const authoringInProgress = Boolean(
    (state.authoringAction &&
      owner?.active &&
      state.authoringActionOwner === owner) ||
    recording.isRecording ||
    recording.finishRequested ||
    recording.isCommitting ||
    (recorded.blocks?.length &&
      owner?.active &&
      (!recorded.owner || recorded.owner === owner)),
  );
  if (state.authoringInProgress !== authoringInProgress) {
    useWorkflowYamlEditorStore.setState({ authoringInProgress });
  }
}

let authoringSubscriptions: Array<() => void> = [];

export function disposeWorkflowAuthoringSubscriptions(): void {
  authoringSubscriptions.forEach((unsubscribe) => unsubscribe());
  authoringSubscriptions = [];
}

export function registerWorkflowAuthoringSubscriptions(): void {
  if (authoringSubscriptions.length) return;
  // Recording and generated blocks can settle outside their launcher.
  authoringSubscriptions = [
    useRecordingStore.subscribe((state, previous) => {
      if (
        state.isRecording !== previous.isRecording ||
        state.finishRequested !== previous.finishRequested ||
        state.isCommitting !== previous.isCommitting
      )
        syncAuthoringInProgress();
    }),
    useRecordedBlocksStore.subscribe((state, previous) => {
      if (state.blocks !== previous.blocks || state.owner !== previous.owner)
        syncAuthoringInProgress();
    }),
  ];
  syncAuthoringInProgress();
}

registerWorkflowAuthoringSubscriptions();
import.meta.hot?.dispose(disposeWorkflowAuthoringSubscriptions);

export async function runWorkflowAuthoringAction(
  action: () => void | Promise<unknown>,
): Promise<boolean> {
  if (refuseMutationDuringYamlCommit() || refuseMutationDuringAuthoring())
    return false;
  const token = Symbol("authoring action");
  useWorkflowYamlEditorStore.setState({
    authoringAction: token,
    authoringActionOwner: useWorkflowYamlEditorStore.getState().editorOwner,
    authoringInProgress: true,
  });
  try {
    await action();
    return true;
  } catch {
    // Mutation hooks display the error; callers need no unhandled rejection.
    return false;
  } finally {
    if (useWorkflowYamlEditorStore.getState().authoringAction === token) {
      useWorkflowYamlEditorStore.setState({
        authoringAction: null,
        authoringActionOwner: null,
      });
      syncAuthoringInProgress();
    }
  }
}

const staleYamlMessage =
  "The workflow changed while YAML was open. Reopen the YAML view to continue.";

export function reconcileYamlDraftAfterGraphChange(
  buildYaml?: () => string,
): void {
  const state = useWorkflowYamlEditorStore.getState();
  if (!state.active) return;
  if (buildYaml && !isWorkflowYamlDirty(state)) {
    try {
      const yaml = buildYaml();
      useWorkflowYamlEditorStore.setState({
        draft: yaml,
        entrySnapshot: yaml,
        stale: false,
        error: null,
      });
      return;
    } catch {
      // An applied workflow must stay applied even if its YAML cannot be rebuilt.
    }
  }
  useWorkflowYamlEditorStore.setState({ stale: true, error: staleYamlMessage });
}

function refuseStaleYamlCommit(): boolean {
  const state = useWorkflowYamlEditorStore.getState();
  if (!state.stale) return false;
  state.setError(staleYamlMessage);
  toast({ title: staleYamlMessage, variant: "destructive" });
  return true;
}

export function isWorkflowYamlDirty(state: {
  draft: string;
  entrySnapshot: string;
}): boolean {
  return state.draft !== state.entrySnapshot;
}

// Fire `onChange` when the editable YAML draft changes while the Code editor is
// active. A draft edit doesn't touch the canvas, so canvas-derived effects miss
// it; subscribing to the draft lets them re-sync. Returns an unsubscribe.
export function subscribeToYamlDraftChanges(onChange: () => void): () => void {
  return useWorkflowYamlEditorStore.subscribe((state, prev) => {
    if (state.active && state.draft !== prev.draft) {
      onChange();
    }
  });
}

// Shared entry point for the commit-on-switch flow used by the overlay's Visual
// toggle, the top-bar save, and the nav-blocker "Save changes" dialog. Guards
// against a re-entrant commit and toggles the committing flag around it.
// Returns false when a commit is already running, none is registered, or the
// draft is invalid.
export async function commitYamlDraft(
  persist: boolean,
  codeCacheDeletionApproved?: boolean,
): Promise<boolean> {
  const store = useWorkflowYamlEditorStore.getState();
  if (
    refuseStaleYamlCommit() ||
    refuseYamlCommitDuringCopilotAcceptance() ||
    refuseMutationDuringAuthoring()
  )
    return false;
  if (store.committing || store.commitInProgress || !store.commit) {
    return false;
  }
  // A programmatic save can start before CodeMirror blurs or debounces.
  store.flushDraft?.();
  store.setCommitting(true);
  let owner = store.commitOwner;
  try {
    const result = store.commit(persist, codeCacheDeletionApproved);
    owner = useWorkflowYamlEditorStore.getState().commitOwner;
    return await result;
  } finally {
    const current = useWorkflowYamlEditorStore.getState();
    if (
      current.commit === store.commit &&
      current.commitOwner === owner &&
      !current.persistingOwner
    ) {
      store.setCommitInProgress(false);
    }
  }
}

export function createYamlCommitOwner(
  workflowPermanentId: string,
): YamlCommitOwner {
  return { workflowPermanentId, active: true };
}

export function invalidateYamlCommitOwner(owner: YamlCommitOwner): void {
  owner.active = false;
  finishYamlCommit(owner);
}

export function beginYamlCommit(owner: YamlCommitOwner): boolean {
  if (refuseStaleYamlCommit()) return false;
  return beginWorkflowTransaction(owner, "yaml");
}

function beginWorkflowTransaction(
  owner: YamlCommitOwner,
  lockKind: "yaml" | "save",
): boolean {
  if (
    !owner.active ||
    refuseYamlCommitDuringCopilotAcceptance() ||
    refuseMutationDuringAuthoring()
  )
    return false;
  const store = useWorkflowYamlEditorStore.getState();
  if (store.commitInProgress || store.pendingSaves[owner.workflowPermanentId])
    return false;
  useWorkflowYamlEditorStore.setState({
    pendingSaves: {
      ...store.pendingSaves,
      [owner.workflowPermanentId]: { owner, kind: lockKind, persisting: false },
    },
    commitOwner: owner,
    commitInProgress: true,
    committing: true,
    lockKind,
  });
  return true;
}

export function finishYamlCommit(owner: YamlCommitOwner): void {
  // Editor cleanup cannot release a save whose request has not settled.
  const state = useWorkflowYamlEditorStore.getState();
  const pending = state.pendingSaves[owner.workflowPermanentId];
  if (
    (pending?.owner === owner &&
      (pending.kind === "save" || pending.persisting)) ||
    state.persistingOwner === owner
  )
    return;
  finishWorkflowTransaction(owner);
}

function finishWorkflowTransaction(owner: YamlCommitOwner): void {
  const state = useWorkflowYamlEditorStore.getState();
  if (state.pendingSaves[owner.workflowPermanentId]?.owner === owner) {
    useWorkflowYamlEditorStore.setState({
      pendingSaves: Object.fromEntries(
        Object.entries(state.pendingSaves).filter(
          ([id]) => id !== owner.workflowPermanentId,
        ),
      ),
    });
  }
  if (state.commitOwner !== owner) return;
  useWorkflowYamlEditorStore.setState((state) => ({
    commitOwner: null,
    persistingOwner: null,
    commitInProgress: false,
    lockKind: state.copilotAcceptance ? "copilot" : null,
    committing: false,
  }));
}

type StoredSave = {
  workflowPermanentId: string;
  baseVersion: number;
  timestamp: number;
};

const saveStorageKey = (workflowPermanentId: string) =>
  `workflow-pending-save:${workflowPermanentId}`;

function readStoredSave(workflowPermanentId: string): StoredSave | null {
  try {
    const raw = sessionStorage.getItem(saveStorageKey(workflowPermanentId));
    if (!raw) return null;
    const stored = JSON.parse(raw) as StoredSave;
    return stored.workflowPermanentId === workflowPermanentId &&
      Number.isFinite(stored.baseVersion) &&
      Number.isFinite(stored.timestamp)
      ? stored
      : null;
  } catch {
    return null;
  }
}

export function markWorkflowSavePersisting(
  owner: YamlCommitOwner,
  baseVersion: number,
): void {
  const state = useWorkflowYamlEditorStore.getState();
  const pending = state.pendingSaves[owner.workflowPermanentId];
  if (pending?.owner !== owner) return;
  let reloadProtectionUnavailable = false;
  try {
    sessionStorage.setItem(
      saveStorageKey(owner.workflowPermanentId),
      JSON.stringify({
        workflowPermanentId: owner.workflowPermanentId,
        baseVersion,
        timestamp: Date.now(),
      } satisfies StoredSave),
    );
  } catch {
    reloadProtectionUnavailable = true;
  }
  useWorkflowYamlEditorStore.setState({
    pendingSaves: {
      ...state.pendingSaves,
      [owner.workflowPermanentId]: {
        ...pending,
        persisting: true,
        reloadProtectionUnavailable,
      },
    },
    ...(state.commitOwner === owner ? { persistingOwner: owner } : {}),
  });
}

export function markWorkflowSaveSettled(owner: YamlCommitOwner): void {
  const state = useWorkflowYamlEditorStore.getState();
  const pending = state.pendingSaves[owner.workflowPermanentId];
  if (pending?.owner !== owner) return;
  try {
    sessionStorage.removeItem(saveStorageKey(owner.workflowPermanentId));
  } catch {
    // A marker that cannot be removed continues to hold Save after reload.
  }
  useWorkflowYamlEditorStore.setState({
    pendingSaves: {
      ...state.pendingSaves,
      [owner.workflowPermanentId]: {
        ...pending,
        persisting: false,
        slow: false,
        reloadProtectionUnavailable: false,
      },
    },
    ...(state.persistingOwner === owner ? { persistingOwner: null } : {}),
  });
}

export function canInitializeWorkflow(workflowPermanentId: string): boolean {
  const state = useWorkflowYamlEditorStore.getState();
  return (
    !state.pendingSaves[workflowPermanentId] &&
    !readStoredSave(workflowPermanentId) &&
    (!state.commitInProgress ||
      (state.commitOwner !== null &&
        state.commitOwner.workflowPermanentId !== workflowPermanentId)) &&
    !state.pendingAccepts[workflowPermanentId] &&
    (!state.copilotAcceptance ||
      (state.editorOwner !== null &&
        state.editorOwner.workflowPermanentId !== workflowPermanentId))
  );
}

type StoredAccept = Pick<CopilotAcceptSnapshot, "chatId" | "acceptAttempt"> & {
  alwaysAccept: boolean;
};
const acceptStorageKey = (workflowPermanentId: string) =>
  `copilot-pending-accept:${workflowPermanentId}`;

export function readStoredAccept(
  workflowPermanentId: string,
): StoredAccept | null {
  try {
    const raw = sessionStorage.getItem(acceptStorageKey(workflowPermanentId));
    if (!raw) return null;
    const stored = JSON.parse(raw) as StoredAccept;
    return typeof stored.chatId === "string" && stored.chatId.length > 0
      ? stored
      : null;
  } catch {
    return null;
  }
}

export function storePendingAccept(
  workflowPermanentId: string,
  value: StoredAccept,
): boolean {
  try {
    sessionStorage.setItem(
      acceptStorageKey(workflowPermanentId),
      JSON.stringify(value),
    );
    return true;
  } catch {
    return false;
  }
}

export function isYamlCommitOwnerCurrent(owner: YamlCommitOwner): boolean {
  return (
    owner.active && useWorkflowYamlEditorStore.getState().commitOwner === owner
  );
}

function refuseYamlCommitDuringCopilotAcceptance(): boolean {
  const store = useWorkflowYamlEditorStore.getState();
  if (!store.copilotAcceptance) return false;
  store.setError("Wait for the Copilot change to finish");
  toast({
    title: "Wait for the Copilot change to finish",
    variant: "destructive",
  });
  return true;
}

export function beginCopilotAcceptance(): symbol | null {
  // Copilot stays conversational while a recording captures; recorded blocks
  // wait for this lock to release before they apply.
  const recording = useRecordingStore.getState();
  const capturing =
    recording.isRecording &&
    !recording.finishRequested &&
    !recording.isCommitting;
  if (
    refuseMutationDuringYamlCommit() ||
    (!capturing && refuseMutationDuringAuthoring())
  )
    return null;
  if (useWorkflowYamlEditorStore.getState().copilotAcceptance) {
    toast({ title: getWorkflowLockMessage(), variant: "destructive" });
    return null;
  }
  const token = Symbol("copilot change");
  useWorkflowYamlEditorStore.setState({
    copilotAcceptance: token,
    lockKind: "copilot",
  });
  return token;
}

export function finishCopilotAcceptance(token: symbol): void {
  for (const [workflowId, pending] of Object.entries(
    useWorkflowYamlEditorStore.getState().pendingAccepts,
  )) {
    if (pending.reservation === token) {
      try {
        sessionStorage.removeItem(acceptStorageKey(workflowId));
      } catch {
        /* A stale marker keeps recovery conservative on reload. */
      }
    }
  }
  useWorkflowYamlEditorStore.setState((state) => ({
    pendingAccepts: Object.fromEntries(
      Object.entries(state.pendingAccepts).filter(
        ([, pending]) => pending.reservation !== token,
      ),
    ),
  }));
  if (useWorkflowYamlEditorStore.getState().copilotAcceptance === token) {
    useWorkflowYamlEditorStore.setState((state) => ({
      copilotAcceptance: null,
      lockKind: state.commitInProgress ? state.lockKind : null,
    }));
  }
}

export function withCopilotAcceptance(
  token: symbol | undefined,
  apply: () => void,
): boolean {
  const reservation = useWorkflowYamlEditorStore.getState().copilotAcceptance;
  if ((token ?? null) !== reservation) return false;
  const previousOwner =
    useWorkflowYamlEditorStore.getState().applyingCopilotOwner;
  useWorkflowYamlEditorStore.setState({ applyingCopilotOwner: token });
  try {
    if (refuseMutationDuringYamlCommit()) return false;
    apply();
    return true;
  } finally {
    if (useWorkflowYamlEditorStore.getState().applyingCopilotOwner === token)
      useWorkflowYamlEditorStore.setState({
        applyingCopilotOwner: previousOwner,
      });
  }
}

export function getWorkflowLockMessage(): string {
  const state = useWorkflowYamlEditorStore.getState();
  if (state.lockKind === "save") return "A save is in progress";
  if (
    state.lockKind === "copilot" ||
    (!state.commitInProgress && state.copilotAcceptance)
  )
    return "Wait for the Copilot change to finish";
  return "A YAML commit is in progress";
}

export function isLockedByOther(
  owner:
    | YamlCommitOwner
    | symbol
    | undefined = useWorkflowYamlEditorStore.getState().applyingCopilotOwner,
): boolean {
  const state = useWorkflowYamlEditorStore.getState();
  return (
    (state.commitInProgress && state.commitOwner !== owner) ||
    (state.copilotAcceptance !== null && state.copilotAcceptance !== owner)
  );
}

export function refuseMutationDuringYamlCommit(): boolean {
  const state = useWorkflowYamlEditorStore.getState();
  if (!state.commitInProgress && !isLockedByOther()) return false;
  toast({ title: getWorkflowLockMessage(), variant: "destructive" });
  return true;
}

export function refuseMutationDuringAuthoring(): boolean {
  if (!useWorkflowYamlEditorStore.getState().authoringInProgress) return false;
  toast({
    title: "Finish the current authoring action first",
    variant: "destructive",
  });
  return true;
}

export function isWorkflowMutation(change: {
  type: string;
  dragging?: boolean;
}): boolean {
  return (
    change.type !== "select" &&
    change.type !== "dimensions" &&
    change.type !== "position"
  );
}

export function filterWorkflowChanges<
  T extends { type: string; dragging?: boolean },
>(changes: T[]): T[] {
  if (!changes.some(isWorkflowMutation) || !refuseMutationDuringYamlCommit())
    return changes;
  return changes.filter((change) => !isWorkflowMutation(change));
}

export function registerEditorOwner(owner: YamlCommitOwner): void {
  const state = useWorkflowYamlEditorStore.getState();
  const previousOwner = state.authoringActionOwner;
  const canResume =
    previousOwner?.workflowPermanentId === owner.workflowPermanentId;
  const pending = state.pendingAccepts[owner.workflowPermanentId];
  const leavingAccept = Object.values(state.pendingAccepts).some(
    (item) => item.reservation === state.copilotAcceptance,
  );
  const copilotAcceptance =
    pending?.reservation ?? (leavingAccept ? null : state.copilotAcceptance);
  let pendingSave = state.pendingSaves[owner.workflowPermanentId];
  if (!pendingSave) {
    const restored = readStoredSave(owner.workflowPermanentId);
    if (restored) {
      pendingSave = {
        owner,
        kind: "save",
        persisting: true,
        slow: true,
        restored,
      };
      useWorkflowYamlEditorStore.setState({
        pendingSaves: {
          ...state.pendingSaves,
          [owner.workflowPermanentId]: pendingSave,
        },
      });
    }
  }
  useWorkflowYamlEditorStore.setState({
    commitOwner: pendingSave?.owner ?? null,
    persistingOwner: pendingSave?.persisting ? pendingSave.owner : null,
    commitInProgress: Boolean(pendingSave),
    committing: Boolean(pendingSave),
    copilotAcceptance,
    lockKind: pendingSave
      ? pendingSave.kind
      : copilotAcceptance
        ? "copilot"
        : null,
    editorOwner: owner,
    authoringAction: canResume ? state.authoringAction : null,
    authoringActionOwner: canResume ? owner : null,
  });
  syncAuthoringInProgress();
}

export function unregisterEditorOwner(owner: YamlCommitOwner): void {
  invalidateYamlCommitOwner(owner);
  if (useWorkflowYamlEditorStore.getState().editorOwner === owner) {
    useWorkflowYamlEditorStore.setState({ editorOwner: null });
    const recorded = useRecordedBlocksStore.getState();
    if (!recorded.owner || recorded.owner === owner)
      recorded.clearRecordedBlocks();
    syncAuthoringInProgress();
  }
}

export function isYamlCommitRevisionCurrent(
  revision: number,
  persisted = false,
): boolean {
  if (useWorkflowYamlEditorStore.getState().revision === revision) return true;
  const message = persisted
    ? SAVE_STALE_MESSAGE
    : "The editor changed while committing; commit again";
  useWorkflowYamlEditorStore.getState().setError(message);
  toast({
    title: message,
    variant: "destructive",
  });
  return false;
}

export async function persistYamlCommitIfCurrent<T>(
  revision: number,
  persist: () => Promise<T>,
  owner?: YamlCommitOwner,
): Promise<false | { response: T }> {
  if (owner && !isYamlCommitOwnerCurrent(owner)) return false;
  if (!isYamlCommitRevisionCurrent(revision)) return false;
  const response = await persist();
  if (owner && !isYamlCommitOwnerCurrent(owner)) return false;
  if (!isYamlCommitRevisionCurrent(revision, true)) return false;
  return { response };
}

export function beginSaveTransaction(owner: YamlCommitOwner): boolean {
  if (useWorkflowYamlEditorStore.getState().editorOwner !== owner) return false;
  return beginWorkflowTransaction(owner, "save");
}

export function finishSaveTransaction(owner: YamlCommitOwner): void {
  finishWorkflowTransaction(owner);
}
