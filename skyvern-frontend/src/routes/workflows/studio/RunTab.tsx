import { useNavigate, useParams } from "react-router-dom";

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
  const urlRunId = useStudioRunId();
  const { workflowPermanentId } = useParams();
  const setCopilotCollapsed = useStudioShellStore((s) => s.setCopilotCollapsed);
  const { data: runs } = useWorkflowRunsQuery({
    workflowPermanentId,
    page: 1,
    pageSize: 1,
  });
  const runId = urlRunId ?? runs?.[0]?.workflow_run_id;

  return (
    <RunView
      workflowRunId={runId}
      onFix={() => setCopilotCollapsed(false)}
      onRetry={() => navigate(`/workflows/${workflowPermanentId}/run`)}
    />
  );
}
