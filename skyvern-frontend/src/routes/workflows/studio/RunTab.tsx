import { useNavigate, useParams, useSearchParams } from "react-router-dom";

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
  const { workflowPermanentId } = useParams();
  const { openPane } = useStudioPanes();
  const { runId, pending } = useStudioInspectedRun();
  // Fix (Copilot) and Retry both mutate/rerun the workflow — gone with the
  // source agent, so the CTAs go too (the run stays viewable).
  const workflowDeleted = useStudioWorkflowDeletedAt() !== null;
  // ?bl= marks a block-scoped run; "Retry as-is" would rerun the whole workflow,
  // so suppress that CTA for block runs (the block is rerun from the editor).
  const isBlockRun = searchParams.has("bl");

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
      onRetry={
        isBlockRun || workflowDeleted
          ? undefined
          : // The post-start navigate resets the layout to the run mapping,
            // so the form round-trip carries nothing.
            () => navigate(`/agents/${workflowPermanentId}/run`)
      }
    />
  );
}
