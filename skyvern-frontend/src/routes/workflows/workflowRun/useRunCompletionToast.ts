import type { Status, WorkflowRunRetryFields } from "@/api/types";
import { toast } from "@/components/ui/use-toast";
import { statusIsAFailureType } from "@/routes/tasks/types";
import { useEffect, useRef } from "react";
import { useBlockRunTarget } from "../editor/hooks/useBlockRunTarget";
import { claimRunCompletionNotice } from "./runCompletionNotices";
import { runIsLogicallyActive, runIsLogicallyFinal } from "./runRetryState";

type RunLike = {
  workflow_run_id: string;
  status: Status;
  failure_reason?: string | null;
} & WorkflowRunRetryFields;

export function useRunCompletionToast(run: RunLike | null | undefined) {
  const { workflowRunId, blockLabel } = useBlockRunTarget();
  const observedActiveRunIds = useRef(new Set<string>());
  useEffect(() => {
    if (!run) return;
    const runId = run.workflow_run_id;
    if (runIsLogicallyActive(run)) {
      observedActiveRunIds.current.add(runId);
      return;
    }
    if (!runIsLogicallyFinal(run) || !observedActiveRunIds.current.has(runId))
      return;

    claimRunCompletionNotice(runId, () => {
      if (workflowRunId === runId && blockLabel !== undefined) {
        const failed = statusIsAFailureType(run);
        toast({
          title: `Agent Block ${blockLabel}: ${run.status}`,
          description: failed ? `Reason: ${run.failure_reason}` : undefined,
          variant: failed ? "destructive" : "success",
        });
      } else if (run.status === "failed" || run.status === "terminated") {
        toast({
          title: "Run Failed",
          description: "The agent run has failed.",
          variant: "destructive",
        });
      } else if (run.status === "timed_out") {
        toast({
          title: "Run Timed Out",
          description: "The agent run has timed out.",
          variant: "destructive",
        });
      } else if (run.status === "canceled") {
        toast({
          title: "Run Canceled",
          description: "The agent run has been canceled.",
          variant: "destructive",
        });
      } else if (run.status === "completed") {
        toast({
          title: "Run Completed",
          description: "The agent run has been completed.",
          variant: "success",
        });
      }
    });
  }, [run, workflowRunId, blockLabel]);
}
