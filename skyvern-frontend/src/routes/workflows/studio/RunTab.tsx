import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { STUDIO_PANES_PARAM } from "./panes";
import { RunView } from "./runview/RunView";
import { useStudioInspectedRun } from "./useStudioInspectedRun";
import { useStudioPanes } from "./useStudioPanes";

/**
 * Timeline pane of the studio shell — the run timeline + run-data view (the
 * Browser pane owns the visuals). Shows the run in the URL when present,
 * otherwise the workflow's most recent run.
 */
export function RunTab() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { workflowPermanentId } = useParams();
  const { openPane, resolveLivePanes } = useStudioPanes();
  const { runId, pending } = useStudioInspectedRun();
  // ?bl= marks a block-scoped run; "Retry as-is" would rerun the whole workflow,
  // so suppress that CTA for block runs (the block is rerun from the editor).
  const isBlockRun = searchParams.has("bl");

  return (
    <RunView
      workflowRunId={runId}
      runIdPending={pending}
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
          : () =>
              // ?panes= rides through the run form so the post-start navigate
              // restores this exact layout (plus the run surfaces appended).
              navigate(
                `/agents/${workflowPermanentId}/run?${STUDIO_PANES_PARAM}=${resolveLivePanes().join(",")}`,
              )
      }
    />
  );
}
