import { useEffect, useRef } from "react";
import {
  clearDeferredEdits,
  getDeferredEditReplayRevision,
} from "@/hooks/useDeferredLockedEdit";
import {
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { canonicalRecoveriesByWorkflow } from "../../copilot/WorkflowCopilotChat";
import type { QueryClient } from "@tanstack/react-query";

import { useMountEffect } from "@/hooks/useMountEffect";

type WorkflowChangesStore = {
  setHasChanges: (hasChanges: boolean) => void;
};

type WorkspaceMountInitializationOptions = {
  cacheKey: string;
  closeWorkflowPanel: () => void;
  queryClient: Pick<QueryClient, "invalidateQueries">;
  workflowChangesStore: WorkflowChangesStore;
  workflowPermanentId?: string;
};

export function useWorkspaceDeferredEditCleanup(workflowId: string) {
  useEffect(() => {
    return () => {
      // Copilot parks recovery in layout cleanup, before this passive cleanup.
      // A remounted recovery may hold a lock or still be waiting for one.
      const state = useWorkflowYamlEditorStore.getState();
      const scalarLockBlocksCleanup =
        (state.editorOwner === null ||
          state.editorOwner.workflowPermanentId === workflowId) &&
        selectEditorMutationLocked(state);
      if (
        !canonicalRecoveriesByWorkflow.has(workflowId) &&
        !state.pendingSaves[workflowId] &&
        !scalarLockBlocksCleanup
      ) {
        clearDeferredEdits(workflowId);
      }
    };
  }, [workflowId]);
}

function useWorkspaceMountInitialization({
  cacheKey,
  closeWorkflowPanel,
  queryClient,
  workflowChangesStore,
  workflowPermanentId,
}: WorkspaceMountInitializationOptions) {
  const replayRevisionAtMount = useRef(
    getDeferredEditReplayRevision(workflowPermanentId),
  );
  useMountEffect(() => {
    // Child layout and passive effects can restore buffered edits before this
    // parent effect runs. Those edits are not part of the saved workflow.
    workflowChangesStore.setHasChanges(
      getDeferredEditReplayRevision(workflowPermanentId) !==
        replayRevisionAtMount.current,
    );
    if (workflowPermanentId) {
      queryClient.invalidateQueries({
        queryKey: ["cache-key-values", workflowPermanentId, cacheKey],
      });
    }
    closeWorkflowPanel();
  });
}

export { useWorkspaceMountInitialization };
