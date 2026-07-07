import { useParams } from "react-router-dom";

import { useGlobalWorkflowsQuery } from "./useGlobalWorkflowsQuery";

// Global (read-only) workflows can't be edited in place — the header offers
// "Make a Copy" instead. Editing surfaces gate on this.
export function useIsGlobalWorkflow(): boolean {
  const { workflowPermanentId } = useParams();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  return Boolean(
    globalWorkflows?.some(
      (w) => w.workflow_permanent_id === workflowPermanentId,
    ),
  );
}
