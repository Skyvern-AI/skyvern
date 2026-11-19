import { Status } from "@/api/types";
import { useWorkflowQuery } from "../../hooks/useWorkflowQuery";
import { useWorkflowRunsQuery } from "../../hooks/useWorkflowRunsQuery";

// Same query keys as useStudioRunSignals and the editor's workflow load, so
// this reads their cache instead of adding a request or a poll.
export function useDocLatestRunCompleted(
  workflowPermanentId: string | null,
): boolean {
  const { data: runs } = useWorkflowRunsQuery({
    workflowPermanentId: workflowPermanentId ?? undefined,
    page: 1,
    pageSize: 1,
  });
  const { data: workflow } = useWorkflowQuery({
    workflowPermanentId: workflowPermanentId ?? undefined,
  });
  const latest = runs?.[0];
  return Boolean(
    latest &&
    latest.status === Status.Completed &&
    !latest.debug_session_id &&
    workflow &&
    latest.workflow_id === workflow.workflow_id,
  );
}
