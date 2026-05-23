import { useReactFlow } from "@xyflow/react";
import { useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { useCallback } from "react";

import { toast } from "@/components/ui/use-toast";
import { useCacheKeyValueStore } from "@/store/CacheKeyValueStore";
import {
  useWorkflowHasChangesStore,
  useWorkflowSave,
} from "@/store/WorkflowHasChangesStore";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { getWorkflowErrors } from "../workflowEditorUtils";
import type { AppNode } from "../nodes";

export function useSaveWorkflow(): () => Promise<void> {
  const { workflowPermanentId } = useParams();
  const reactFlow = useReactFlow<AppNode>();
  const saveWorkflow = useWorkflowSave({ status: "published" });
  const workflowChangesStore = useWorkflowHasChangesStore();
  const setFilter = useCacheKeyValueStore((s) => s.setFilter);
  const queryClient = useQueryClient();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const cacheKey = workflow?.cache_key ?? "";

  return useCallback(async () => {
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

    await saveWorkflow.mutateAsync();

    workflowChangesStore.setSaidOkToCodeCacheDeletion(false);

    queryClient.invalidateQueries({
      queryKey: ["cache-key-values", workflowPermanentId, cacheKey],
    });

    setFilter(null);
  }, [
    reactFlow,
    saveWorkflow,
    workflowChangesStore,
    queryClient,
    workflowPermanentId,
    cacheKey,
    setFilter,
  ]);
}
