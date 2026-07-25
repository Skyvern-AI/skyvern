import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { useWorkflowRunsQuery } from "../hooks/useWorkflowRunsQuery";
import { finalizedRunStatus } from "./runProjections";
import { useStudioRunId } from "./useStudioRunId";

/**
 * The run facts the shell keys UI off: Run-tab gating, the toggle status dot,
 * and the first-visit pane default. `knownHasRuns` stays undefined until the
 * runs page-1 probe has data, so callers can tell "no runs" from "not loaded".
 */
export function useStudioRunSignals() {
  const urlRunId = useStudioRunId();
  const workflowPermanentId = useWorkflowPermanentId();
  const { data: urlRun } = useWorkflowRunWithWorkflowQuery(
    urlRunId ? { workflowRunId: urlRunId } : undefined,
  );
  const { data: runs } = useWorkflowRunsQuery({
    workflowPermanentId,
    page: 1,
    pageSize: 1,
  });
  const knownHasRuns = runs === undefined ? undefined : runs.length > 0;
  return {
    hasRun: Boolean(urlRunId) || knownHasRuns === true,
    runStatus: finalizedRunStatus(
      urlRunId ? urlRun?.status : runs?.[0]?.status,
    ),
    knownHasRuns,
  };
}
