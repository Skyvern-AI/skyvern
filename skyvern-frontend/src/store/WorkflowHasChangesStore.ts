import { useRecordedBlocksStore } from "./RecordedBlocksStore";
import { useRecordingStore } from "./useRecordingStore";
import { useWorkflowParametersStore } from "./WorkflowParametersStore";
import { getInitialParameters } from "@/routes/workflows/editor/utils";
import {
  selectEditorMutationLocked,
  SAVE_STALE_MESSAGE,
  beginSaveTransaction,
  canInitializeWorkflow,
  finishSaveTransaction,
  getWorkflowLockMessage,
  isYamlCommitOwnerCurrent,
  markWorkflowSavePersisting,
  markWorkflowSaveSettled,
  refuseMutationDuringYamlCommit,
  useWorkflowYamlEditorStore,
  type YamlCommitContext,
  type YamlCommitOwner,
} from "./WorkflowYamlEditorStore";
import { applyYamlCommitMetadata } from "./WorkflowTitleStore";
import { AxiosError } from "axios";
import { useEffect, useRef } from "react";
import { create } from "zustand";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { stringify as convertToYAML } from "yaml";
import { usePostHog } from "posthog-js/react";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { flushBufferedEditorEdits } from "@/hooks/useDeferredLockedEdit";
import { buildWorkflowSaveRequest } from "@/routes/workflows/editor/workflowYamlDocument";
import { YamlCommitError } from "@/routes/workflows/editor/workflowVersionFromSaveData";
import {
  type BlockYAML,
  type ParameterYAML,
} from "@/routes/workflows/types/workflowYamlTypes";
import type {
  WorkflowApiResponse,
  WorkflowSettings,
} from "@/routes/workflows/types/workflowTypes";
type SaveData = {
  parameters: Array<ParameterYAML>;
  blocks: Array<BlockYAML>;
  workflowDefinitionVersion: number;
  title: string;
  description: string | null;
  settings: WorkflowSettings;
  workflow: WorkflowApiResponse;
};

type WorkflowHasChangesStore = {
  getSaveData: () => SaveData | null;
  hydrateSavedSettings:
    | ((
        workflow: WorkflowApiResponse,
        options?: { hydrateGraph?: boolean },
      ) => void)
    | null;
  hasChanges: boolean;
  saveIsPending: boolean;
  saveGeneration: number;
  saveGenerationsByWorkflow: Record<string, number>;
  recordPersistedSave: (workflowPermanentId?: string) => void;
  // Why workflow saves are held (an Accept whose server outcome is unconfirmed), or null.
  saveBlockedReason: string | null;
  saidOkToCodeCacheDeletion: boolean;
  showConfirmCodeCacheDeletion: boolean;
  pendingRecordingId: string | null;
  pendingRecordingWorkflowPermanentId: string | null;
  // Reference-counted flag: multiple concurrent internal updates won't
  // accidentally clear each other. Gate on > 0 in consumers.
  internalUpdateCount: number;
  setGetSaveData: (getSaveData: () => SaveData) => void;
  setHasChanges: (
    hasChanges: boolean,
    options?: { fromYamlCommit?: boolean },
  ) => void;
  setSaveIsPending: (isPending: boolean) => void;
  setSaveBlockedReason: (reason: string | null) => void;
  setSaidOkToCodeCacheDeletion: (saidOkToCodeCacheDeletion: boolean) => void;
  setShowConfirmCodeCacheDeletion: (show: boolean) => void;
  setPendingRecording: (
    recordingId: string,
    workflowPermanentId: string,
  ) => void;
  clearPendingRecording: (recordingId: string) => void;
  beginInternalUpdate: () => void;
  endInternalUpdate: () => void;
};

interface WorkflowSaveOpts {
  status?: string;
}

const deleteDiscardedRecordingCallbacks = new Set<
  (recordingId: string) => void
>();

function assertWorkflowSaveAllowed(
  yamlCommit?: YamlCommitContext,
  saveOwner?: YamlCommitOwner | null,
): void {
  const blockedReason = useWorkflowHasChangesStore.getState().saveBlockedReason;
  if (blockedReason) {
    toast({
      title: "Save is paused",
      description: blockedReason,
      variant: "destructive",
    });
    throw new YamlCommitError("workflow", blockedReason);
  }
  const state = useWorkflowYamlEditorStore.getState();
  if (state.authoringInProgress) {
    throw new YamlCommitError(
      "workflow",
      "Finish the current authoring action before saving",
    );
  }
  const owner = state.lockKind === "save" ? saveOwner : yamlCommit?.owner;
  if (
    state.copilotAcceptance ||
    (state.commitInProgress && (!owner || !isYamlCommitOwnerCurrent(owner)))
  ) {
    throw new SaveRefusedError(getWorkflowLockMessage());
  }
}

const useWorkflowHasChangesStore = create<WorkflowHasChangesStore>(
  (set, get) => {
    let cancelInitializationRetry: (() => void) | undefined;
    return {
      hasChanges: false,
      saveIsPending: false,
      saveBlockedReason: null,
      saveGeneration: 0,
      saveGenerationsByWorkflow: {},
      recordPersistedSave: (
        workflowPermanentId = get().getSaveData()?.workflow
          .workflow_permanent_id,
      ) =>
        set((state) => ({
          saveGeneration: state.saveGeneration + 1,
          saveGenerationsByWorkflow: workflowPermanentId
            ? {
                ...state.saveGenerationsByWorkflow,
                [workflowPermanentId]: state.saveGeneration + 1,
              }
            : state.saveGenerationsByWorkflow,
        })),
      saidOkToCodeCacheDeletion: false,
      showConfirmCodeCacheDeletion: false,
      pendingRecordingId: null,
      pendingRecordingWorkflowPermanentId: null,
      internalUpdateCount: 0,
      getSaveData: () => null,
      hydrateSavedSettings: null,
      setGetSaveData: (getSaveData: () => SaveData) => {
        set({ getSaveData });
      },
      setHasChanges: (hasChanges: boolean, options) => {
        const { commitInProgress, commitOwner, editorOwner } =
          useWorkflowYamlEditorStore.getState();
        // Navigation can mount a new editor owner before the old owner's PUT settles.
        if (
          !options?.fromYamlCommit &&
          !hasChanges &&
          commitInProgress &&
          editorOwner?.active &&
          commitOwner &&
          editorOwner !== commitOwner
        ) {
          cancelInitializationRetry?.();
          cancelInitializationRetry = useWorkflowYamlEditorStore.subscribe(
            (state) => {
              if (state.editorOwner !== editorOwner || !editorOwner.active) {
                cancelInitializationRetry?.();
                cancelInitializationRetry = undefined;
              } else if (!selectEditorMutationLocked(state)) {
                cancelInitializationRetry?.();
                cancelInitializationRetry = undefined;
                get().setHasChanges(false);
              }
            },
          );
          return;
        }
        if (!options?.fromYamlCommit && refuseMutationDuringYamlCommit())
          return;
        cancelInitializationRetry?.();
        cancelInitializationRetry = undefined;
        if (hasChanges) useWorkflowYamlEditorStore.getState().bumpRevision();
        // Recording attachment is part of the unsaved editor draft. Every caller
        // that accepts or discards that draft by clearing the dirty flag also
        // discards its pending attachment handshake.
        set((state) => {
          if (!hasChanges && state.pendingRecordingId !== null) {
            deleteDiscardedRecordingCallbacks
              .values()
              .next()
              .value?.(state.pendingRecordingId);
          }
          return hasChanges
            ? { hasChanges }
            : {
                hasChanges,
                pendingRecordingId: null,
                pendingRecordingWorkflowPermanentId: null,
              };
        });
      },
      setSaveIsPending: (isPending: boolean) => {
        set({ saveIsPending: isPending });
      },
      setSaveBlockedReason: (reason) => {
        set({ saveBlockedReason: reason });
      },
      setSaidOkToCodeCacheDeletion: (saidOkToCodeCacheDeletion: boolean) => {
        set({ saidOkToCodeCacheDeletion });
      },
      setShowConfirmCodeCacheDeletion: (show: boolean) => {
        set({ showConfirmCodeCacheDeletion: show });
      },
      setPendingRecording: (recordingId, workflowPermanentId) => {
        set((state) =>
          state.pendingRecordingId === null ||
          (state.pendingRecordingId === recordingId &&
            state.pendingRecordingWorkflowPermanentId === workflowPermanentId)
            ? {
                pendingRecordingId: recordingId,
                pendingRecordingWorkflowPermanentId: workflowPermanentId,
              }
            : {},
        );
      },
      clearPendingRecording: (recordingId) => {
        set((state) =>
          state.pendingRecordingId === recordingId
            ? {
                pendingRecordingId: null,
                pendingRecordingWorkflowPermanentId: null,
              }
            : {},
        );
      },
      beginInternalUpdate: () => {
        set((state) => ({
          internalUpdateCount: state.internalUpdateCount + 1,
        }));
      },
      endInternalUpdate: () => {
        set((state) => ({
          internalUpdateCount: Math.max(0, state.internalUpdateCount - 1),
        }));
      },
    };
  },
);

export class SaveRefusedError extends Error {
  constructor(message = getWorkflowLockMessage()) {
    super(message);
    this.name = "SaveRefusedError";
  }
}

export class SaveStaleError extends Error {
  constructor() {
    super(SAVE_STALE_MESSAGE);
    this.name = "SaveStaleError";
  }
}

const WORKFLOW_SAVE_NOTICE_MS = 30_000;

const useWorkflowSave = (opts?: WorkflowSaveOpts) => {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const postHog = usePostHog();
  const {
    getSaveData,
    setHasChanges,
    setSaveIsPending,
    setShowConfirmCodeCacheDeletion,
  } = useWorkflowHasChangesStore();

  const commitInProgress = useWorkflowYamlEditorStore(
    (state) => state.commitInProgress,
  );
  useEffect(() => {
    const deleteRecording = (recordingId: string) => {
      void getClient(credentialGetter, "sans-api-v1")
        .then((client) => client.delete(`/browser_recordings/${recordingId}`))
        .catch(() => undefined);
    };
    deleteDiscardedRecordingCallbacks.add(deleteRecording);
    return () => {
      deleteDiscardedRecordingCallbacks.delete(deleteRecording);
    };
  }, [credentialGetter]);

  const saveWorkflowMutation = useMutation({
    mutationFn: async (
      override?: Partial<SaveData> & {
        yamlCommit?: YamlCommitContext;
        codeCacheDeletionApproved?: boolean;
      },
    ) => {
      const changes = useWorkflowHasChangesStore.getState();
      const codeCacheDeletionApproved =
        override?.codeCacheDeletionApproved ??
        changes.saidOkToCodeCacheDeletion;
      if (override?.codeCacheDeletionApproved === undefined)
        changes.setSaidOkToCodeCacheDeletion(false);
      assertWorkflowSaveAllowed(override?.yamlCommit);
      if (
        override?.yamlCommit &&
        !isYamlCommitOwnerCurrent(override.yamlCommit.owner)
      )
        return;
      if (!override?.yamlCommit) {
        useWorkflowYamlEditorStore.getState().flushDraft?.();
        flushBufferedEditorEdits();
      }
      const store = useWorkflowYamlEditorStore.getState();
      const owner =
        override?.yamlCommit?.owner ??
        useWorkflowYamlEditorStore.getState().editorOwner;
      if (!override?.yamlCommit && (!owner || !beginSaveTransaction(owner))) {
        throw new SaveRefusedError(
          store.copilotAcceptance
            ? "Wait for the Copilot change to finish"
            : !owner
              ? "The workflow editor is no longer available"
              : undefined,
        );
      }
      const revision = useWorkflowYamlEditorStore.getState().revision;
      let uncertain = false;
      try {
        const base = useWorkflowHasChangesStore.getState().getSaveData();

        if (!base)
          throw new SaveRefusedError(
            "The workflow editor is no longer available",
          );
        if (
          owner &&
          base.workflow.workflow_permanent_id !== owner.workflowPermanentId
        ) {
          throw new SaveRefusedError(
            "The workflow editor changed before saving",
          );
        }
        // YAML-mode saves pass the parsed draft (blocks/parameters/version, plus
        // a corrected finally_block_label) so we persist the edit directly
        // instead of the graph, which lags a commit's async setNodes.
        const saveData: SaveData = override ? { ...base, ...override } : base;

        const client = await getClient(credentialGetter);
        assertWorkflowSaveAllowed(override?.yamlCommit, owner);
        if (
          override?.yamlCommit &&
          !isYamlCommitOwnerCurrent(override.yamlCommit.owner)
        )
          return;
        if (
          owner &&
          (!isYamlCommitOwnerCurrent(owner) ||
            useWorkflowYamlEditorStore.getState().revision !== revision)
        ) {
          throw new SaveRefusedError(
            "The workflow editor changed before saving",
          );
        }
        const requestBody = buildWorkflowSaveRequest(saveData, opts);
        const changesState = useWorkflowHasChangesStore.getState();
        const recordingId =
          changesState.pendingRecordingWorkflowPermanentId ===
          saveData.workflow.workflow_permanent_id
            ? changesState.pendingRecordingId
            : null;
        if (recordingId !== null) requestBody.recording_id = recordingId;

        const yaml = convertToYAML(requestBody);

        if (owner) markWorkflowSavePersisting(owner, saveData.workflow.version);
        const noticeTimer = setTimeout(() => {
          if (!owner) return;
          const state = useWorkflowYamlEditorStore.getState();
          const pending = state.pendingSaves[owner.workflowPermanentId];
          if (pending?.owner !== owner || !pending.persisting) return;
          useWorkflowYamlEditorStore.setState({
            pendingSaves: {
              ...state.pendingSaves,
              [owner.workflowPermanentId]: { ...pending, slow: true },
            },
          });
        }, WORKFLOW_SAVE_NOTICE_MS);
        let response;
        try {
          response = await client.put<WorkflowApiResponse>(
            `/workflows/${saveData.workflow.workflow_permanent_id}`,
            yaml,
            {
              headers: {
                "Content-Type": "text/plain",
              },
              params: {
                delete_code_cache_is_ok: codeCacheDeletionApproved
                  ? "true"
                  : "false",
              },
            },
          );
        } catch (error) {
          if (!(error as AxiosError | null)?.response) {
            uncertain = true;
            if (owner) {
              const state = useWorkflowYamlEditorStore.getState();
              const pending = state.pendingSaves[owner.workflowPermanentId];
              if (pending?.owner === owner)
                useWorkflowYamlEditorStore.setState({
                  pendingSaves: {
                    ...state.pendingSaves,
                    [owner.workflowPermanentId]: { ...pending, slow: true },
                  },
                });
            }
          } else if (owner && !owner.active) {
            const loaded = useWorkflowHasChangesStore
              .getState()
              .getSaveData()?.workflow;
            if (
              loaded?.workflow_permanent_id === owner.workflowPermanentId &&
              hydrateWorkflowEditor(loaded)
            )
              setHasChanges(false, { fromYamlCommit: true });
          }
          throw error;
        } finally {
          clearTimeout(noticeTimer);
        }
        if (recordingId !== null)
          useWorkflowHasChangesStore
            .getState()
            .clearPendingRecording(recordingId);
        if (
          !owner ||
          owner.active ||
          useWorkflowYamlEditorStore.getState().editorOwner
            ?.workflowPermanentId === saveData.workflow.workflow_permanent_id
        )
          useWorkflowHasChangesStore
            .getState()
            .recordPersistedSave(saveData.workflow.workflow_permanent_id);
        const activeOwner = useWorkflowYamlEditorStore.getState().editorOwner;
        if (
          owner &&
          !owner.active &&
          activeOwner?.active &&
          activeOwner.workflowPermanentId === owner.workflowPermanentId &&
          hydrateWorkflowEditor(response.data)
        ) {
          if (recordingId !== null)
            useWorkflowHasChangesStore
              .getState()
              .clearPendingRecording(recordingId);
          queryClient.setQueryData(
            ["workflow", saveData.workflow.workflow_permanent_id],
            response.data,
          );
          setHasChanges(false, { fromYamlCommit: true });
        }
        if (owner && !override?.yamlCommit) {
          // An unmounted editor cannot accept metadata, but its acknowledged
          // write still invalidates that workflow's cache.
          if (owner.active) {
            const state = useWorkflowYamlEditorStore.getState();
            const recording = useRecordingStore.getState();
            // Generated blocks wait for this save's lock before editing the graph.
            const authoringIsDeferred =
              Boolean(useRecordedBlocksStore.getState().blocks?.length) &&
              !state.authoringAction &&
              !recording.isRecording &&
              !recording.finishRequested &&
              !recording.isCommitting;
            if (
              !isYamlCommitOwnerCurrent(owner) ||
              (state.authoringInProgress && !authoringIsDeferred)
            )
              throw new SaveStaleError();
            if (useWorkflowYamlEditorStore.getState().revision !== revision) {
              const error = new SaveStaleError();
              useWorkflowYamlEditorStore.getState().setError(error.message);
              throw error;
            }
            useWorkflowHasChangesStore
              .getState()
              .hydrateSavedSettings?.(response.data);
            applyYamlCommitMetadata(response.data, {}, true);
            setHasChanges(false, { fromYamlCommit: true });
          }
          if (owner.active) {
            void queryClient.invalidateQueries({
              queryKey: ["workflow", base.workflow.workflow_permanent_id],
            });
            void queryClient.invalidateQueries({ queryKey: ["workflows"] });
            void queryClient.invalidateQueries({
              queryKey: ["block-scripts", base.workflow.workflow_permanent_id],
            });
          }
        }
        return response;
      } finally {
        if (owner && !uncertain) {
          markWorkflowSaveSettled(owner);
          if (!override?.yamlCommit || !owner.active)
            finishSaveTransaction(owner);
        }
      }
    },
    onMutate: (override) => {
      const store = useWorkflowYamlEditorStore.getState();
      const owner = override?.yamlCommit?.owner ?? store.editorOwner;
      return {
        owner,
        revision: store.revision,
        workflowPermanentId: owner?.workflowPermanentId,
      };
    },
    onSuccess: (_response, override, context) => {
      if (context?.owner && !context.owner.active) return;
      if (
        override?.yamlCommit &&
        !isYamlCommitOwnerCurrent(override.yamlCommit.owner)
      )
        return;
      if (
        override?.yamlCommit &&
        useWorkflowYamlEditorStore.getState().revision !==
          override.yamlCommit.revision
      )
        return;

      const saveData = getSaveData();

      if (!saveData) {
        return;
      }

      postHog.capture("builder.workflow.saved", {
        org_id: saveData.workflow.organization_id,
        workflow_permanent_id: saveData.workflow.workflow_permanent_id,
        block_count: saveData.blocks.length,
        block_types: saveData.blocks.map((b) => b.block_type),
      });

      toast({
        title: "Changes saved",
        description: "Your changes have been saved",
        variant: "success",
      });
    },
    onError: (
      error: AxiosError | YamlCommitError | SaveRefusedError | SaveStaleError,
      override,
      context,
    ) => {
      if (context?.owner && !context.owner.active) return;
      if (
        override?.yamlCommit &&
        !isYamlCommitOwnerCurrent(override.yamlCommit.owner)
      )
        return;
      if (
        error instanceof SaveRefusedError ||
        error instanceof SaveStaleError
      ) {
        toast({ title: error.message, variant: "destructive" });
        return;
      }
      if (error instanceof YamlCommitError) {
        toast({
          title: "Failed to save agent",
          description: error.message,
          variant: "destructive",
        });
        return;
      }
      if (
        context?.owner &&
        useWorkflowYamlEditorStore.getState().pendingSaves[
          context.owner.workflowPermanentId
        ]?.persisting
      )
        return;
      const responseData = error.response?.data as
        | {
            detail?:
              | string
              | Array<{
                  loc?: Array<string | number>;
                  msg?: string;
                  type?: string;
                }>;
          }
        | undefined;
      const rawDetail = responseData?.detail;

      if (
        typeof rawDetail === "string" &&
        rawDetail.startsWith("No confirmation for code cache deletion")
      ) {
        setShowConfirmCodeCacheDeletion(true);
        return;
      }

      let description: string;
      if (typeof rawDetail === "string" && rawDetail) {
        description = rawDetail;
      } else if (Array.isArray(rawDetail) && rawDetail.length > 0) {
        // FastAPI's own 422 responses (e.g. request body validation) return detail
        // as an array; our custom ValidationError handler returns it as a string.
        description = rawDetail
          .map((err) => {
            const loc = err.loc
              ?.filter((part) => part !== "body" && part !== "__root__")
              .join(" -> ");
            return loc ? `${loc}: ${err.msg}` : (err.msg ?? "Unknown error");
          })
          .join("; ");
      } else {
        description =
          "Failed to save agent. Please check your agent configuration and try again.";
      }

      toast({
        title: "Failed to save agent",
        description,
        variant: "destructive",
      });
    },
    onSettled: (_response, _error, _override, context) => {
      if (
        context?.owner &&
        !context.owner.active &&
        context.workflowPermanentId
      ) {
        // The cache outlives the editor, and a failed response can still follow
        // a persisted update. Reopening must fetch the submitted workflow.
        for (const queryKey of [
          ["workflow", context.workflowPermanentId],
          ["workflows"],
          ["block-scripts", context.workflowPermanentId],
        ])
          void queryClient.invalidateQueries({ queryKey });
      }
    },
  });

  useEffect(() => {
    setSaveIsPending(commitInProgress);
  }, [commitInProgress, setSaveIsPending]);

  return saveWorkflowMutation;
};

function hydrateWorkflowEditor(workflow: WorkflowApiResponse): boolean {
  const owner = useWorkflowYamlEditorStore.getState().editorOwner;
  const hydrate = useWorkflowHasChangesStore.getState().hydrateSavedSettings;
  if (
    !owner?.active ||
    owner.workflowPermanentId !== workflow.workflow_permanent_id ||
    !hydrate
  )
    return false;
  hydrate(workflow, { hydrateGraph: true });
  applyYamlCommitMetadata(workflow, {}, true);
  return true;
}

function hydrateRestoredWorkflowSave(
  workflow: WorkflowApiResponse,
  owner: YamlCommitOwner,
): void {
  const state = useWorkflowYamlEditorStore.getState();
  const pending = state.pendingSaves[owner.workflowPermanentId];
  if (
    !pending?.restored ||
    pending.owner !== owner ||
    workflow.workflow_permanent_id !== owner.workflowPermanentId ||
    state.editorOwner?.workflowPermanentId !== owner.workflowPermanentId
  )
    return;
  if (!hydrateWorkflowEditor(workflow)) return;
  useWorkflowHasChangesStore
    .getState()
    .setHasChanges(false, { fromYamlCommit: true });
  markWorkflowSaveSettled(owner);
  finishSaveTransaction(owner);
}

export function discardRestoredWorkflowSave(
  owner: YamlCommitOwner,
  workflow: WorkflowApiResponse,
): void {
  hydrateRestoredWorkflowSave(workflow, owner);
}

export function usePendingWorkflowSaveRecovery(
  workflow: WorkflowApiResponse,
): void {
  const pending = useWorkflowYamlEditorStore(
    (state) => state.pendingSaves[workflow.workflow_permanent_id],
  );
  const hydrate = useWorkflowHasChangesStore(
    (state) => state.hydrateSavedSettings,
  );
  useEffect(() => {
    // Only a reload loses the original PUT response. Live requests retain their reservation.
    if (
      !pending?.restored ||
      !hydrate ||
      !(workflow.version > pending.restored.baseVersion)
    )
      return;
    hydrateRestoredWorkflowSave(workflow, pending.owner);
  }, [workflow, pending, hydrate]);
}

export {
  useWorkflowSave,
  useWorkflowHasChangesStore,
  type SaveData as WorkflowSaveData,
};
export function useHydrateWorkflowParameters(
  workflow: WorkflowApiResponse | undefined,
  routeWorkflowPermanentId: string | undefined,
): void {
  const hydratedWorkflowId = useRef<string | null>(null);
  const observedWorkflow = useRef<WorkflowApiResponse>();
  const hydrationLocked = useWorkflowYamlEditorStore(() =>
    routeWorkflowPermanentId
      ? !canInitializeWorkflow(routeWorkflowPermanentId)
      : true,
  );
  const setParameters = useWorkflowParametersStore(
    (state) => state.setParameters,
  );
  useEffect(() => {
    if (
      hydrationLocked ||
      !workflow ||
      workflow.workflow_permanent_id !== routeWorkflowPermanentId
    )
      return;
    // Unlocking a save must not replay the unchanged pre-save response.
    const workflowChanged = observedWorkflow.current !== workflow;
    if (
      useWorkflowParametersStore.getState().parametersWorkflowPermanentId !==
        routeWorkflowPermanentId &&
      (hydratedWorkflowId.current !== routeWorkflowPermanentId ||
        (workflowChanged && !useWorkflowHasChangesStore.getState().hasChanges))
    ) {
      if (
        setParameters(getInitialParameters(workflow), {
          workflowPermanentId: routeWorkflowPermanentId,
        })
      ) {
        hydratedWorkflowId.current = routeWorkflowPermanentId;
        observedWorkflow.current = workflow;
      }
    } else {
      observedWorkflow.current = workflow;
    }
  }, [workflow, routeWorkflowPermanentId, setParameters, hydrationLocked]);
}
