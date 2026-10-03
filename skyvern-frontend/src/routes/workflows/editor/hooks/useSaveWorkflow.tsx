import { useReactFlow } from "@xyflow/react";
import { useQueryClient } from "@tanstack/react-query";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useCallback } from "react";

import { toast } from "@/components/ui/use-toast";
import { flushBufferedEditorEdits } from "@/hooks/useDeferredLockedEdit";
import { useCacheKeyValueStore } from "@/store/CacheKeyValueStore";
import {
  SaveRefusedError,
  SaveStaleError,
  useWorkflowHasChangesStore,
  useWorkflowSave,
} from "@/store/WorkflowHasChangesStore";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import {
  selectEditorMutationLocked,
  SAVE_STALE_MESSAGE,
  commitYamlDraft,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { getWorkflowErrors } from "../workflowEditorUtils";
import type { AppNode } from "../nodes";

export class SaveFailedError extends Error {
  constructor() {
    super("The workflow was not saved.");
    this.name = "SaveFailedError";
  }
}

export function useSaveWorkflow(): () => Promise<void> {
  const workflowPermanentId = useWorkflowPermanentId();
  const reactFlow = useReactFlow<AppNode>();
  const saveWorkflow = useWorkflowSave({ status: "published" });
  const setFilter = useCacheKeyValueStore((s) => s.setFilter);
  const queryClient = useQueryClient();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const cacheKey = workflow?.cache_key ?? "";

  return useCallback(async () => {
    const changes = useWorkflowHasChangesStore.getState();
    const codeCacheDeletionApproved = changes.saidOkToCodeCacheDeletion;
    changes.setSaidOkToCodeCacheDeletion(false);
    // While the YAML editor is open, saving persists the parsed draft directly
    // rather than the stale pre-edit canvas — committing applies the graph via
    // async setNodes, so a graph-based save here would race it.
    if (useWorkflowYamlEditorStore.getState().active) {
      if (!(await commitYamlDraft(true, codeCacheDeletionApproved))) {
        const state = useWorkflowYamlEditorStore.getState();
        if (state.error === SAVE_STALE_MESSAGE) throw new SaveStaleError();
        if (selectEditorMutationLocked(state)) {
          const error = new SaveRefusedError(state.error ?? undefined);
          toast({ title: error.message, variant: "destructive" });
          throw error;
        }
        throw new SaveFailedError();
      }
      return;
    }

    useWorkflowYamlEditorStore.getState().flushDraft?.();
    flushBufferedEditorEdits();
    const nodes = reactFlow.getNodes();
    const errors = getWorkflowErrors(nodes);
    if (errors.length > 0) {
      toast({
        title: "Encountered error while trying to save workflow:",
        description: (
          <div className="space-y-2">
            {errors.map((error) => (
              <p key={error}>{error}</p>
            ))}
          </div>
        ),
        variant: "destructive",
      });
      throw new SaveFailedError();
    }

    await saveWorkflow.mutateAsync({ codeCacheDeletionApproved });

    queryClient.invalidateQueries({
      queryKey: ["cache-key-values", workflowPermanentId, cacheKey],
    });

    setFilter(null);
  }, [
    reactFlow,
    saveWorkflow,
    queryClient,
    workflowPermanentId,
    cacheKey,
    setFilter,
  ]);
}

export async function confirmCodeCacheDeletion(
  onSave: () => Promise<void>,
): Promise<boolean> {
  const changes = useWorkflowHasChangesStore.getState();
  changes.setSaidOkToCodeCacheDeletion(true);
  try {
    await onSave();
    changes.setShowConfirmCodeCacheDeletion(false);
    return true;
  } catch (error) {
    if (error instanceof SaveRefusedError || error instanceof SaveStaleError) {
      changes.setShowConfirmCodeCacheDeletion(false);
    }
    return false;
  }
}
