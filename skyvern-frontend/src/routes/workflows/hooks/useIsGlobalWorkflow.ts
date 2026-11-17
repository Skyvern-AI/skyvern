import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import { useGlobalWorkflowsQuery } from "./useGlobalWorkflowsQuery";

// Global (read-only) workflows can't be edited in place — the header offers
// "Make a Copy" instead. Editing surfaces gate on this.
export function useIsGlobalWorkflow(): boolean {
  return useGlobalWorkflowStatus() ?? false;
}

// undefined until the global-workflows list loads; a failed load reads as
// not global.
export function useGlobalWorkflowStatus(): boolean | undefined {
  const workflowPermanentId = useWorkflowPermanentId();
  const { data: globalWorkflows, isPending } = useGlobalWorkflowsQuery();
  if (isPending) return undefined;
  return Boolean(
    globalWorkflows?.some(
      (w) => w.workflow_permanent_id === workflowPermanentId,
    ),
  );
}
