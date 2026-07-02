import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useWorkflowRunsQuery } from "../hooks/useWorkflowRunsQuery";
import { RunView } from "./runview/RunView";
import { useStudioPanes } from "./useStudioPanes";
import { useStudioRunId } from "./useStudioRunId";

/**
 * Run pane of the studio shell — the fused hero + filmstrip RunView. Shows the
 * run in the URL when present, otherwise the workflow's most recent run.
 */
export function RunTab() {
  const navigate = useNavigate();
  const urlRunId = useStudioRunId();
  const [searchParams] = useSearchParams();
  const { workflowPermanentId } = useParams();
  const { openPane } = useStudioPanes();
  // isPending (not isLoading) stays true while the query is disabled waiting for
  // globalWorkflows, so the empty state can't flash before the run lookup settles.
  const { data: runs, isPending: runsPending } = useWorkflowRunsQuery({
    workflowPermanentId,
    page: 1,
    pageSize: 1,
  });
  const runId = urlRunId ?? runs?.[0]?.workflow_run_id;
  // ?bl= marks a block-scoped run; "Retry as-is" would rerun the whole workflow,
  // so suppress that CTA for block runs (the block is rerun from the editor).
  const isBlockRun = searchParams.has("bl");

  return (
    <RunView
      workflowRunId={runId}
      runIdPending={!urlRunId && runsPending}
      onFix={(seedMessage) => {
        // One replace-navigation opens the Copilot pane and seeds the message via
        // location.state (Workspace reads it as the copilot's initialMessage), so
        // the pane write can't race a separate state-only navigation.
        openPane("copilot", {
          state: { copilotMessage: seedMessage, copilotFixOrigin: true },
        });
      }}
      onRetry={
        isBlockRun
          ? undefined
          : () => navigate(`/agents/${workflowPermanentId}/run`)
      }
    />
  );
}
