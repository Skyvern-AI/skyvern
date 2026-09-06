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
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery({
    workflowRunId: runId,
  });
  // Fix (Copilot) and Retry both mutate/rerun the workflow — gone with the
  // source agent, so the CTAs go too (the run stays viewable).
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;
  // ?bl= marks a block-scoped run; "Retry" would rerun the whole workflow,
  // so suppress that CTA for block runs (the block is rerun from the editor).
  const isBlockRun = searchParams.has("bl");
  const retryPath = `/agents/${workflowPermanentId}/run`;
  const retryState =
    workflowRun &&
    statusIsFinalized(workflowRun) &&
    workflowRun.task_v2 === null
      ? getRerunNavigationState(workflowRun)
      : undefined;
  const retryRun = () => {
    if (retryState) {
      navigate(retryPath, { state: retryState });
      return;
    }
    navigate(retryPath);
  };

  return (
    <RunView
      workflowRunId={runId}
      runIdPending={pending}
      onFix={
        workflowDeleted || !runId
          ? undefined
          : (failingLabel) => {
              // One replace-navigation opens the pane and arms the action via
              // location.state, so the pane write can't race a state-only navigation.
              openPane("copilot", {
                state: {
                  copilotAction: {
                    kind: "diagnose_run",
                    workflowRunId: runId,
                    nonce: crypto.randomUUID(),
                  },
                },
                selectedBlockLabel: failingLabel ?? null,
              });
            }
      }
      onRetry={isBlockRun || workflowDeleted ? undefined : retryRun}
      milestoneRerun={
        isBlockRun || workflowDeleted
          ? undefined
          : { to: retryPath, state: retryState }
      }
    />
  );
}
