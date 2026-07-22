import { useReactFlow } from "@xyflow/react";
import { useQueryClient } from "@tanstack/react-query";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useCallback } from "react";

import { toast } from "@/components/ui/use-toast";
import { useCacheKeyValueStore } from "@/store/CacheKeyValueStore";
import { useWorkflowSave } from "@/store/WorkflowHasChangesStore";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import {
  commitYamlDraft,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { getWorkflowErrors } from "../workflowEditorUtils";
import type { AppNode } from "../nodes";

export function useSaveWorkflow(): () => Promise<void> {
  const workflowPermanentId = useWorkflowPermanentId();
  const reactFlow = useReactFlow<AppNode>();
  const saveWorkflow = useWorkflowSave({ status: "published" });
  const setFilter = useCacheKeyValueStore((s) => s.setFilter);
  const queryClient = useQueryClient();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const cacheKey = workflow?.cache_key ?? "";

  return useCallback(async () => {
    // While the YAML editor is open, saving persists the parsed draft directly
    // rather than the stale pre-edit canvas — committing applies the graph via
    // async setNodes, so a graph-based save here would race it.
    if (useWorkflowYamlEditorStore.getState().active) {
      await commitYamlDraft(true);
      return;
    }

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
      return;
    }

    await saveWorkflow.mutateAsync(undefined);

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
