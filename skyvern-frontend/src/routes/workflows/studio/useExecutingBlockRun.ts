import { useParams, useSearchParams } from "react-router-dom";

import { Status } from "@/api/types";
import { statusIsFinalized } from "@/routes/tasks/types";

import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";

/**
 * True while a block-scoped run (?wr= + ?bl=) is executing inside the current
 * debug session: the agent is driving the shared debug browser, so the stream
 * flags that the user is co-driving. Paused runs don't count — a pause can be
 * waiting on human input (2FA, verification).
 */
export function useExecutingBlockRun(): boolean {
  const { workflowPermanentId } = useParams();
  const [searchParams] = useSearchParams();
  const isBlockRun = searchParams.has("bl");
  const urlRunId = searchParams.get("wr");
  const workflowRunId = isBlockRun && urlRunId ? urlRunId : undefined;
  // Shares the run cache RunView/StudioSpine poll (5s while not finalized), so
  // the gate releases on terminal status even with the Timeline pane closed.
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(
    workflowRunId ? { workflowRunId } : undefined,
  );
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: false,
  });
  if (!workflowRunId || !workflowRun) {
    return false;
  }
  return (
    !statusIsFinalized(workflowRun) &&
    workflowRun.status !== Status.Paused &&
    workflowRun.browser_session_id != null &&
    workflowRun.browser_session_id === debugSession?.browser_session_id
  );
}
