import {
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";

import { useStudioShellStore } from "@/store/StudioShellStore";

import { useWorkflowRunsQuery } from "../hooks/useWorkflowRunsQuery";
import { RunView } from "./runview/RunView";
import { useStudioRunId } from "./useStudioRunId";

/**
 * Run tab of the studio shell — the fused hero + filmstrip RunView. Shows the
 * run in the URL when present, otherwise the workflow's most recent run.
 */
export function RunTab() {
  const navigate = useNavigate();
  const location = useLocation();
  const urlRunId = useStudioRunId();
  const [searchParams] = useSearchParams();
  const { workflowPermanentId } = useParams();
  const setCopilotCollapsed = useStudioShellStore((s) => s.setCopilotCollapsed);
  const { data: runs } = useWorkflowRunsQuery({
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
      onFix={(seedMessage) => {
        // Seed via location.state (Workspace reads it as the copilot's initialMessage);
        // replace so Fix adds no Back-able entry and the message can't re-fire on Back.
        navigate(`${location.pathname}${location.search}`, {
          state: { copilotMessage: seedMessage },
          replace: true,
        });
        setCopilotCollapsed(false);
      }}
      onRetry={
        isBlockRun
          ? undefined
          : () => navigate(`/workflows/${workflowPermanentId}/run`)
      }
    />
  );
}
