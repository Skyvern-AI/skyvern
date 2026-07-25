import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import { useWorkflowRunsQuery } from "../hooks/useWorkflowRunsQuery";
import { useStudioRunId } from "./useStudioRunId";

/**
 * The run the studio's visual surfaces inspect: the run named in the URL when
 * present, otherwise the workflow's most recent run (same fallback as RunTab).
 */
export function useStudioInspectedRun(): {
  runId: string | undefined;
  // True when the URL names the run; false for the latest-run fallback.
  explicit: boolean;
  pending: boolean;
} {
  const urlRunId = useStudioRunId();
  const workflowPermanentId = useWorkflowPermanentId();
  const { data: runs, isPending } = useWorkflowRunsQuery({
    workflowPermanentId,
    page: 1,
    pageSize: 1,
    // The latest-run fallback is only needed when the URL names no run.
    enabled: !urlRunId,
  });
  return {
    runId: urlRunId ?? runs?.[0]?.workflow_run_id,
    explicit: Boolean(urlRunId),
    pending: !urlRunId && isPending,
  };
}
