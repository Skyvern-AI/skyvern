import { create } from "zustand";
import type { MetadataPatch } from "@/routes/workflows/editor/workflowVersionFromSaveData";
import type { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import {
  refuseMutationDuringYamlCommit,
  canInitializeWorkflow,
  reconcileYamlDraftAfterGraphChange,
  useWorkflowYamlEditorStore,
} from "./WorkflowYamlEditorStore";

const DEFAULT_WORKFLOW_TITLE = "New Agent" as const;
// "New Workflow" is the backend's placeholder; both mean "nobody has named this yet".
const DEFAULT_WORKFLOW_TITLES: readonly string[] = [
  DEFAULT_WORKFLOW_TITLE,
  "New Workflow",
];

// "" is the pre-hydration state, before initializeTitle runs — an unnamed agent for
// every purpose here, so a title arriving mid-hydration is applied rather than dropped.
const isDefaultTitle = (title: string) =>
  title.trim() === "" || DEFAULT_WORKFLOW_TITLES.includes(title.trim());

type MetadataEditOptions = { fromYamlCommit?: boolean; source?: "workflow" };

type WorkflowTitleStore = {
  emberMetadataWorkflowId: string | null;
  emberMetadataEdits: Record<
    string,
    // Undefined covers edits made before history identifies the pending proposal.
    {
      proposal: string | null | undefined;
      edits: MetadataPatch;
      graphEdited?: true;
    }
  >;
  trackEmberMetadata: (
    workflowPermanentId: string,
    proposal: string | null | undefined,
  ) => void;
  startEmberMetadata: (workflowPermanentId: string) => void;
  clearEmberMetadata: (workflowPermanentId: string) => void;
  recordEmberGraphEdit: () => void;
  title: string;
  titleWorkflowPermanentId: string | null;
  description: string | null;
  descriptionWorkflowPermanentId: string | null;
  initializeDescription: (
    workflowPermanentId: string,
    description: string | null,
  ) => void;
  resetDescriptionSession: (workflowPermanentId: string) => void;
  setDescriptionFromWorkflow: (
    description: string | null,
    options?: MetadataEditOptions,
  ) => void;
  setDescriptionFromUser: (
    description: string | null,
    options?: MetadataEditOptions,
  ) => void;
  titleHasBeenGenerated: boolean;
  titleGeneration: number;
  restoreTitle: (title: string, generated: boolean) => void;
  isNewTitle: () => boolean;
  setTitle: (title: string, options?: MetadataEditOptions) => void;
  setTitleFromGeneration: (title: string) => void;
  setTitleFromEmberIfDefault: (title: string) => void;
  syncTitleFromWorkflow: (title: string, options?: MetadataEditOptions) => void;
  initializeTitle: (title: string, workflowPermanentId?: string) => void;
  resetTitleSession: (workflowPermanentId: string) => void;
  resetTitle: () => void;
};

const useWorkflowTitleStore = create<WorkflowTitleStore>((set, get) => {
  const reconcileMetadata = (options?: MetadataEditOptions) => {
    if (!options?.fromYamlCommit) reconcileYamlDraftAfterGraphChange();
  };
  const recordUserMetadata = (
    patch: MetadataPatch,
    options?: MetadataEditOptions,
  ) => {
    if (options?.source === "workflow") return;
    const state = get();
    const workflowId =
      useWorkflowYamlEditorStore.getState().editorOwner?.workflowPermanentId ??
      state.emberMetadataWorkflowId;
    if (!workflowId || !state.emberMetadataEdits[workflowId]) return;
    set({
      emberMetadataEdits: {
        ...state.emberMetadataEdits,
        [workflowId]: {
          ...state.emberMetadataEdits[workflowId],
          edits: { ...state.emberMetadataEdits[workflowId].edits, ...patch },
        },
      },
    });
  };
  return {
    emberMetadataWorkflowId: null,
    emberMetadataEdits: {},
    startEmberMetadata: (workflowPermanentId) =>
      set((state) => ({
        emberMetadataWorkflowId: workflowPermanentId,
        emberMetadataEdits: {
          ...state.emberMetadataEdits,
          [workflowPermanentId]: state.emberMetadataEdits[
            workflowPermanentId
          ] ?? { proposal: undefined, edits: {} },
        },
      })),
    recordEmberGraphEdit: () => {
      const state = get();
      const workflowId =
        useWorkflowYamlEditorStore.getState().editorOwner?.workflowPermanentId;
      const previous = workflowId && state.emberMetadataEdits[workflowId];
      if (!workflowId || !previous || previous.graphEdited) return;
      set({
        emberMetadataEdits: {
          ...state.emberMetadataEdits,
          [workflowId]: { ...previous, graphEdited: true },
        },
      });
    },
    clearEmberMetadata: (workflowPermanentId) =>
      set((state) => ({
        emberMetadataEdits: Object.fromEntries(
          Object.entries(state.emberMetadataEdits).filter(
            ([id]) => id !== workflowPermanentId,
          ),
        ),
        emberMetadataWorkflowId:
          state.emberMetadataWorkflowId === workflowPermanentId
            ? null
            : state.emberMetadataWorkflowId,
      })),
    trackEmberMetadata: (workflowPermanentId, proposal) =>
      set((state) => {
        const previous = state.emberMetadataEdits[workflowPermanentId];
        return {
          emberMetadataWorkflowId: workflowPermanentId,
          emberMetadataEdits: {
            ...state.emberMetadataEdits,
            [workflowPermanentId]: {
              proposal,
              ...(previous?.graphEdited &&
              (previous.proposal === proposal ||
                previous.proposal === undefined)
                ? { graphEdited: true as const }
                : {}),
              edits:
                previous &&
                (previous.proposal === proposal ||
                  previous.proposal === undefined)
                  ? previous.edits
                  : {},
            },
          },
        };
      }),
    title: "",
    titleWorkflowPermanentId: null,
    description: null,
    descriptionWorkflowPermanentId: null,
    initializeDescription: (workflowPermanentId, description) => {
      if (get().descriptionWorkflowPermanentId === workflowPermanentId) return;
      if (!canInitializeWorkflow(workflowPermanentId)) return;
      reconcileMetadata();
      set({ descriptionWorkflowPermanentId: workflowPermanentId, description });
    },
    resetDescriptionSession: (workflowPermanentId) => {
      if (get().descriptionWorkflowPermanentId === workflowPermanentId) {
        set({ descriptionWorkflowPermanentId: null });
      }
    },
    setDescriptionFromWorkflow: (description, options) => {
      if (!options?.fromYamlCommit && refuseMutationDuringYamlCommit()) return;
      if (description !== get().description) reconcileMetadata(options);
      set({ description });
    },
    setDescriptionFromUser: (description, options) => {
      if (!options?.fromYamlCommit && refuseMutationDuringYamlCommit()) return;
      if ((description || null) !== get().description)
        reconcileMetadata(options);
      recordUserMetadata({ description: description || null }, options);
      set({ description: description || null });
    },
    titleHasBeenGenerated: false,
    titleGeneration: 0,
    restoreTitle: (title, generated) => {
      if (refuseMutationDuringYamlCommit()) return;
      set((state) => ({
        title,
        titleHasBeenGenerated: generated,
        titleGeneration: state.titleGeneration + 1,
      }));
    },
    isNewTitle: () => {
      return isDefaultTitle(get().title);
    },
    setTitle: (title: string, options) => {
      // The title bar remains mounted during YAML persistence; only the commit
      // itself may apply metadata until that transaction releases the lock.
      if (!options?.fromYamlCommit && refuseMutationDuringYamlCommit()) return;
      if (title.trim() !== get().title) reconcileMetadata(options);
      recordUserMetadata({ title: title.trim() }, options);
      set({
        title: title.trim(),
        titleHasBeenGenerated: !isDefaultTitle(title),
      });
    },
    // Automatic canvas syncs (canonical load, snap-back) replay a snapshot that can
    // carry the placeholder the agent has since been named past. Nothing automatic may
    // un-name an agent; only the user's own rename can.
    syncTitleFromWorkflow: (title: string, options) => {
      if (!options?.fromYamlCommit && refuseMutationDuringYamlCommit()) return;
      if (isDefaultTitle(title) && !isDefaultTitle(get().title)) {
        return;
      }
      if (title.trim() !== get().title) reconcileMetadata(options);
      set({
        title: title.trim(),
        titleHasBeenGenerated: !isDefaultTitle(title),
      });
    },
    setTitleFromGeneration: (title: string) => {
      if (refuseMutationDuringYamlCommit()) return;
      if (title.trim() !== get().title) reconcileMetadata();
      set({ title: title.trim(), titleHasBeenGenerated: true });
    },
    initializeTitle: (title, workflowPermanentId) => {
      // Views without an editable workflow session still need forced hydration.
      if (
        workflowPermanentId !== undefined &&
        get().titleWorkflowPermanentId === workflowPermanentId
      )
        return;
      if (
        workflowPermanentId
          ? !canInitializeWorkflow(workflowPermanentId)
          : refuseMutationDuringYamlCommit()
      )
        return;
      if (title.trim() !== get().title) reconcileMetadata();
      set({
        title: title.trim(),
        titleWorkflowPermanentId: workflowPermanentId ?? null,
        titleHasBeenGenerated: !isDefaultTitle(title),
      });
    },
    resetTitleSession: (workflowPermanentId) => {
      if (get().titleWorkflowPermanentId === workflowPermanentId) {
        set({ titleWorkflowPermanentId: null });
      }
    },
    // An Ember push must not overwrite a name the user chose, and a push that still
    // carries a placeholder must not count as "generated" — that flag permanently
    // disarms the auto-titler.
    setTitleFromEmberIfDefault: (title: string) => {
      if (refuseMutationDuringYamlCommit()) return;
      if (!isDefaultTitle(get().title)) {
        return;
      }
      if (title.trim() !== get().title) reconcileMetadata();
      set({
        title: title.trim(),
        titleHasBeenGenerated: !isDefaultTitle(title),
      });
    },
    resetTitle: () => {
      if (refuseMutationDuringYamlCommit()) return;
      if (get().title || get().description) reconcileMetadata();
      set({
        title: "",
        titleWorkflowPermanentId: null,
        description: null,
        descriptionWorkflowPermanentId: null,
        titleHasBeenGenerated: false,
      });
    },
  };
});

useWorkflowTitleStore.subscribe((state, previous) => {
  if (
    state.title !== previous.title ||
    state.description !== previous.description
  ) {
    useWorkflowYamlEditorStore.getState().bumpRevision();
  }
});

export function applyYamlCommitMetadata(
  workflow: Pick<WorkflowApiResponse, "title" | "description"> &
    Partial<Pick<WorkflowApiResponse, "workflow_permanent_id">>,
  metadataPatch: MetadataPatch,
  persisted: boolean,
): void {
  const store = useWorkflowTitleStore.getState();
  const options = { fromYamlCommit: true };
  if (persisted) {
    store.setTitle(workflow.title, { ...options, source: "workflow" });
  } else if (metadataPatch.title !== undefined) {
    store.setTitle(metadataPatch.title, options);
  } else {
    store.syncTitleFromWorkflow(workflow.title, options);
  }
  if (
    !persisted &&
    Object.prototype.hasOwnProperty.call(metadataPatch, "description")
  ) {
    store.setDescriptionFromUser(metadataPatch.description ?? null, options);
  } else {
    store.setDescriptionFromWorkflow(workflow.description, options);
  }
  if (persisted && workflow.workflow_permanent_id) {
    useWorkflowTitleStore.setState({
      titleWorkflowPermanentId: workflow.workflow_permanent_id,
      descriptionWorkflowPermanentId: workflow.workflow_permanent_id,
    });
  }
}

export { useWorkflowTitleStore };
