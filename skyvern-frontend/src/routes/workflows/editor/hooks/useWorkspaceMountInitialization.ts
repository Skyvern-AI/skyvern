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

function useWorkspaceMountInitialization({
  cacheKey,
  closeWorkflowPanel,
  queryClient,
  workflowChangesStore,
  workflowPermanentId,
}: WorkspaceMountInitializationOptions) {
  useMountEffect(() => {
    workflowChangesStore.setHasChanges(false);
    if (workflowPermanentId) {
      queryClient.invalidateQueries({
        queryKey: ["cache-key-values", workflowPermanentId, cacheKey],
      });
    }
    closeWorkflowPanel();
  });
}

export { useWorkspaceMountInitialization };
