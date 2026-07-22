import { useNavigate, useSearchParams } from "react-router-dom";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import { statusIsFinalized } from "@/routes/tasks/types";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { getRerunNavigationState } from "../utils";
import { RunView } from "./runview/RunView";
import { useStudioInspectedRun } from "./useStudioInspectedRun";
import { useStudioPanes } from "./useStudioPanes";
import { useStudioWorkflowDeletedAt } from "./StudioShellContext";

/**
 * Overview pane of the studio shell — the run timeline + run-data view (the
 * Browser pane owns the visuals). Shows the run in the URL when present,
 * otherwise the workflow's most recent run.
 */
export function RunTab() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const workflowPermanentId = useWorkflowPermanentId();
  const { openPane } = useStudioPanes();
  const { runId, pending } = useStudioInspectedRun();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(
    runId ? { workflowRunId: runId } : undefined,
  );
  // Fix (Copilot) and Retry both mutate/rerun the workflow — gone with the
  // source agent, so the CTAs go too (the run stays viewable).
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;
  // ?bl= marks a block-scoped run; "Retry as-is" would rerun the whole workflow,
  // so suppress that CTA for block runs (the block is rerun from the editor).
  const isBlockRun = searchParams.has("bl");
  const retryRun = () => {
    const path = `/agents/${workflowPermanentId}/run`;
    if (
      workflowRun &&
      workflowRun.workflow_run_id === runId &&
      statusIsFinalized(workflowRun) &&
      workflowRun.task_v2 === null
    ) {
      navigate(path, { state: getRerunNavigationState(workflowRun) });
      return;
    }
    navigate(path);
  };

  return (
    <RunView
      workflowRunId={runId}
      runIdPending={pending}
      onFix={
        workflowDeleted
          ? undefined
          : (seedMessage) => {
              // One replace-navigation opens the Copilot pane and seeds the message
              // via location.state (Workspace reads it as the copilot's
              // initialMessage), so the pane write can't race a separate
              // state-only navigation.
              openPane("copilot", {
                state: { copilotMessage: seedMessage, copilotFixOrigin: true },
              });
            }
      }
      onRetry={isBlockRun || workflowDeleted ? undefined : retryRun}
    />
  );
}
