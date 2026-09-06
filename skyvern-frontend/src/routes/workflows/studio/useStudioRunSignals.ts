import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { useWorkflowRunsQuery } from "../hooks/useWorkflowRunsQuery";
import { finalizedRunStatus } from "./runProjections";
import { useStudioRunId } from "./useStudioRunId";

/**
 * The run facts the shell keys UI off: Run-tab gating, the toggle status dot
 * and label, and the first-visit pane default. `runId` is the inspected run —
 * the URL's run or the latest-run fallback — so the tab's label and status dot
 * always describe the same run. `knownHasRuns` stays undefined until the runs
 * page-1 probe has data, so callers can tell "no runs" from "not loaded".
 */
export function useStudioRunSignals() {
  const urlRunId = useStudioRunId();
  const workflowPermanentId = useWorkflowPermanentId();
  const { data: urlRun } = useWorkflowRunWithWorkflowQuery({
    workflowRunId: urlRunId,
  });
  const { data: runs } = useWorkflowRunsQuery({
    workflowPermanentId,
    page: 1,
    pageSize: 1,
  });
  const knownHasRuns = runs === undefined ? undefined : runs.length > 0;
  return {
    hasRun: Boolean(urlRunId) || knownHasRuns === true,
    runId: urlRunId ?? runs?.[0]?.workflow_run_id,
    runStatus: finalizedRunStatus(
      urlRunId ? urlRun?.status : runs?.[0]?.status,
    ),
    knownHasRuns,
  };
}
