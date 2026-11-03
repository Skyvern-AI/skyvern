import { useFirstParam } from "@/hooks/useFirstParam";
import { useWorkflowRunByIdQuery } from "./useWorkflowRunQuery";

// The key is required so that passing an options object always states a run, even
// when that run is undefined; omitting the object entirely is what defers to the
// route. Optional-key typing made those two cases identical to tsc.
function useWorkflowRunWithWorkflowQuery(options?: {
  workflowRunId: string | undefined;
  enabled?: boolean;
}) {
  const urlWorkflowRunId = useFirstParam("workflowRunId", "runId");
  const workflowRunId = options ? options.workflowRunId : urlWorkflowRunId;
  return useWorkflowRunByIdQuery(workflowRunId, options?.enabled ?? true);
}

export { useWorkflowRunWithWorkflowQuery };
