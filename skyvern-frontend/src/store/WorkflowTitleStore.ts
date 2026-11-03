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
  copilotMetadataWorkflowId: string | null;
  copilotMetadataEdits: Record<
    string,
    // Undefined covers edits made before history identifies the pending proposal.
    {
      proposal: string | null | undefined;
      edits: MetadataPatch;
      graphEdited?: true;
    }
  >;
  trackCopilotMetadata: (
    workflowPermanentId: string,
    proposal: string | null | undefined,
  ) => void;
  startCopilotMetadata: (workflowPermanentId: string) => void;
  clearCopilotMetadata: (workflowPermanentId: string) => void;
  recordCopilotGraphEdit: () => void;
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
  setTitleFromCopilotIfDefault: (title: string) => void;
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
      state.copilotMetadataWorkflowId;
    if (!workflowId || !state.copilotMetadataEdits[workflowId]) return;
    set({
      copilotMetadataEdits: {
        ...state.copilotMetadataEdits,
        [workflowId]: {
          ...state.copilotMetadataEdits[workflowId],
          edits: { ...state.copilotMetadataEdits[workflowId].edits, ...patch },
        },
      },
    });
  };
  return {
    copilotMetadataWorkflowId: null,
    copilotMetadataEdits: {},
    startCopilotMetadata: (workflowPermanentId) =>
      set((state) => ({
        copilotMetadataWorkflowId: workflowPermanentId,
        copilotMetadataEdits: {
          ...state.copilotMetadataEdits,
          [workflowPermanentId]: state.copilotMetadataEdits[
            workflowPermanentId
          ] ?? { proposal: undefined, edits: {} },
        },
      })),
    recordCopilotGraphEdit: () => {
      const state = get();
      const workflowId =
        useWorkflowYamlEditorStore.getState().editorOwner?.workflowPermanentId;
      const previous = workflowId && state.copilotMetadataEdits[workflowId];
      if (!workflowId || !previous || previous.graphEdited) return;
      set({
        copilotMetadataEdits: {
          ...state.copilotMetadataEdits,
          [workflowId]: { ...previous, graphEdited: true },
        },
      });
    },
    clearCopilotMetadata: (workflowPermanentId) =>
      set((state) => ({
        copilotMetadataEdits: Object.fromEntries(
          Object.entries(state.copilotMetadataEdits).filter(
            ([id]) => id !== workflowPermanentId,
          ),
        ),
        copilotMetadataWorkflowId:
          state.copilotMetadataWorkflowId === workflowPermanentId
            ? null
            : state.copilotMetadataWorkflowId,
      })),
    trackCopilotMetadata: (workflowPermanentId, proposal) =>
      set((state) => {
        const previous = state.copilotMetadataEdits[workflowPermanentId];
        return {
          copilotMetadataWorkflowId: workflowPermanentId,
          copilotMetadataEdits: {
            ...state.copilotMetadataEdits,
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
    // A Copilot push must not overwrite a name the user chose, and a push that still
    // carries a placeholder must not count as "generated" — that flag permanently
    // disarms the auto-titler.
    setTitleFromCopilotIfDefault: (title: string) => {
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
