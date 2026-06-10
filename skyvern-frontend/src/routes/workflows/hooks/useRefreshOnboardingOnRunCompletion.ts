import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Status } from "@/api/types";
import { statusIsFinalized } from "@/routes/tasks/types";

type RunLike = { workflow_run_id: string; status: Status };

// The backend stamps first_run_at only when a run reaches a final status, so
// refresh onboarding once per finalized run so the provider can observe the
// milestone and emit first_run_completed. This includes runs already final on
// first observation (a fast validation/proxy failure, or opening a completed
// run); a repeat invalidation is a no-op since the provider only emits on the
// null -> set transition. Repeat renders of the same run do not refetch.
function useRefreshOnboardingOnRunCompletion(
  workflowRun: RunLike | undefined,
): void {
  const queryClient = useQueryClient();
  const refreshedRunRef = useRef<string | null>(null);

  useEffect(() => {
    if (!workflowRun || !statusIsFinalized(workflowRun)) {
      return;
    }
    const runId = workflowRun.workflow_run_id;
    if (refreshedRunRef.current === runId) {
      return;
    }
    refreshedRunRef.current = runId;
    queryClient.invalidateQueries({ queryKey: ["userOnboarding"] });
  }, [workflowRun, queryClient]);
}

export { useRefreshOnboardingOnRunCompletion };
