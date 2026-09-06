import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Status } from "@/api/types";
import { statusIsFinalized } from "@/routes/tasks/types";
import type { OnboardingStateResponse } from "@/store/onboarding/types";

type RunLike = { workflow_run_id: string; status: Status };

// The backend stamps first_run_at only when a run reaches a final status, so
// refresh onboarding once per finalized run. Failed runs share one delayed
// retry per mount when the first response races the server-side experiment
// assignment. This includes runs already final on first observation; repeat
// renders of the same run do not refetch.
function useRefreshOnboardingOnRunCompletion(
  workflowRun: RunLike | undefined,
): void {
  const queryClient = useQueryClient();
  const refreshedRunRef = useRef<string | null>(null);
  const retryTimeoutRef = useRef<number | undefined>(undefined);
  const retriedMissingAssignmentRef = useRef(false);

  useEffect(
    () => () => {
      window.clearTimeout(retryTimeoutRef.current);
    },
    [],
  );

  useEffect(() => {
    if (!workflowRun || !statusIsFinalized(workflowRun)) {
      return;
    }
    const runId = workflowRun.workflow_run_id;
    if (refreshedRunRef.current === runId) {
      return;
    }
    refreshedRunRef.current = runId;
    window.clearTimeout(retryTimeoutRef.current);
    retryTimeoutRef.current = undefined;
    const invalidation = queryClient.invalidateQueries({
      queryKey: ["userOnboarding"],
    });
    if (
      workflowRun.status !== Status.Failed &&
      workflowRun.status !== Status.Terminated &&
      workflowRun.status !== Status.TimedOut
    ) {
      return;
    }
    void invalidation.then(() => {
      if (refreshedRunRef.current !== runId) {
        return;
      }
      // The onboarding provider keys its cache by the cloud user id, but this hook also runs in
      // OSS where Clerk has no provider. Read the existing query family after invalidation rather
      // than importing Clerk just to reconstruct a cache key.
      const hasAssignmentForRun = queryClient
        .getQueriesData<OnboardingStateResponse>({
          queryKey: ["userOnboarding"],
        })
        .some(
          ([, data]) =>
            data?.recovery_guidance_assignment?.eligible_run_id === runId,
        );
      if (hasAssignmentForRun) {
        return;
      }
      if (retriedMissingAssignmentRef.current) {
        return;
      }
      retriedMissingAssignmentRef.current = true;
      retryTimeoutRef.current = window.setTimeout(() => {
        retryTimeoutRef.current = undefined;
        void queryClient.invalidateQueries({ queryKey: ["userOnboarding"] });
      }, 4_000);
    });
  }, [workflowRun, queryClient]);
}

export { useRefreshOnboardingOnRunCompletion };
