import { runIsLogicallyActive } from "@/routes/workflows/workflowRun/runRetryState";
import { isAxiosError } from "axios";
import { useCallback } from "react";
import { type CredentialGetter, getClient } from "@/api/AxiosClient";
import { isForbiddenError } from "@/api/forbidden";
import {
  Status,
  WorkflowRunRetryFields,
  WorkflowRunStatusApiResponseWithWorkflow,
} from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useFirstParam } from "@/hooks/useFirstParam";

import {
  DefaultError,
  keepPreviousData,
  useQueries,
  useQuery,
} from "@tanstack/react-query";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";

const RUN_STATUS_POLL_INTERVAL_MS = 5000;
// Generous against the poll interval because the gap is measured from the last success, and sleep,
// hidden-tab timer throttling, and retry backoff all widen that gap before an outage even starts.
const POLL_OUTAGE_BUDGET_MS = 120000;
const RUN_STATUS_OUTAGE_RETRY_INTERVAL_MS = 30000;

// Data from before a failed refetch is retained. Poll frequently through a
// short outage, then retry at a quieter cadence until the run status resolves.
// This keeps reconciliation automatic without retrying a persistent error every
// five seconds. The outage is measured from the last success rather than from
// fetchFailureCount, which query-core resets at the start of every fetch.
function getRunStatusRefetchInterval(state: {
  status: "pending" | "error" | "success";
  data?: { status: Status } & WorkflowRunRetryFields;
  dataUpdatedAt: number;
  errorUpdatedAt: number;
}): number | false {
  if (!state.data) {
    return false;
  }
  if (!runIsLogicallyActive(state.data)) {
    return false;
  }
  if (
    state.status === "error" &&
    state.errorUpdatedAt - state.dataUpdatedAt > POLL_OUTAGE_BUDGET_MS
  ) {
    return RUN_STATUS_OUTAGE_RETRY_INTERVAL_MS;
  }
  return RUN_STATUS_POLL_INTERVAL_MS;
}

async function fetchWorkflowRunById(
  credentialGetter: CredentialGetter | null,
  workflowRunId: string | undefined,
  signal: AbortSignal,
): Promise<WorkflowRunStatusApiResponseWithWorkflow> {
  const client = await getClient(credentialGetter, "sans-api-v1");
  return client
    .get(`/workflows/runs/${encodeURIComponent(workflowRunId ?? "")}`, {
      signal,
    })
    .then((response) => response.data);
}

type RunStatusQuery = {
  state: Parameters<typeof getRunStatusRefetchInterval>[0];
};

// Every reader of this route shares one cache entry. A second key over the same
// run builds the whole response again per view and doubles the status poll.
function workflowRunByIdQueryOptions(
  credentialGetter: CredentialGetter | null,
  workflowRunId: string | undefined,
  activeOrgQueryKeyScope: ReturnType<typeof getActiveOrgQueryKeyScope>,
) {
  return {
    queryKey: getOrgScopedQueryKey(
      ["workflowRun", workflowRunId],
      activeOrgQueryKeyScope,
    ),
    // The by-run-id route still serves a run whose workflow version was retired
    // (a Copilot test version), where the per-workflow route answers 404.
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fetchWorkflowRunById(credentialGetter, workflowRunId, signal),
    refetchInterval: (query: RunStatusQuery) =>
      getRunStatusRefetchInterval(query.state),
    // required for OS-level notifications to work (workflow run completion)
    refetchIntervalInBackground: true,
    refetchOnMount: (query: RunStatusQuery) =>
      query.state.data && runIsLogicallyActive(query.state.data)
        ? ("always" as const)
        : false,
    refetchOnWindowFocus: (query: RunStatusQuery) =>
      !!query.state.data && runIsLogicallyActive(query.state.data),
  };
}

function useWorkflowRunByIdQuery(
  workflowRunId: string | undefined,
  enabled: boolean,
) {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);
  // A fresh arrow each render defeats query-core's select memo, re-running the
  // comparison and a deep equality check over the whole payload every time.
  const selectRequestedRun = useCallback(
    (run: WorkflowRunStatusApiResponseWithWorkflow) =>
      run.workflow_run_id === workflowRunId ? run : undefined,
    [workflowRunId],
  );

  return useQuery<
    WorkflowRunStatusApiResponseWithWorkflow,
    DefaultError,
    WorkflowRunStatusApiResponseWithWorkflow | undefined
  >({
    ...workflowRunByIdQueryOptions(
      credentialGetter,
      workflowRunId,
      activeOrgQueryKeyScope,
    ),
    placeholderData: keepPreviousData,
    // keepPreviousData serves the previous run's payload whenever the requested run
    // changes or clears, so withhold it rather than present it as the run in view.
    select: selectRequestedRun,
    enabled: enabled && !!workflowRunId,
  });
}

// The key is required so that passing an options object always states a run, even
// when that run is undefined; omitting the object entirely is what defers to the
// route. Optional-key typing made those two cases identical to tsc.
function useWorkflowRunQuery(options?: { workflowRunId: string | undefined }) {
  const urlWorkflowRunId = useFirstParam("workflowRunId", "runId");
  const workflowRunId = options ? options.workflowRunId : urlWorkflowRunId;
  const workflowPermanentId = useWorkflowPermanentId();
  return useWorkflowRunByIdQuery(workflowRunId, !!workflowPermanentId);
}

type WatchedRunState = RunStatusQuery["state"] & { error?: unknown };

// A watched run whose first read failed is still owed a status, unless it is missing or forbidden.
function getWatchedRunRefetchInterval(state: WatchedRunState): number | false {
  if (!state.data && state.status === "error") {
    const missing =
      isAxiosError(state.error) && state.error.response?.status === 404;
    return missing || isForbiddenError(state.error)
      ? false
      : RUN_STATUS_OUTAGE_RETRY_INTERVAL_MS;
  }
  return getRunStatusRefetchInterval(state);
}

function useWorkflowRunQueries(workflowRunIds: readonly string[]) {
  const credentialGetter = useCredentialGetter();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(useActiveOrgId());
  const workflowPermanentId = useWorkflowPermanentId();
  return useQueries({
    queries: workflowRunIds.map((workflowRunId) => ({
      ...workflowRunByIdQueryOptions(
        credentialGetter,
        workflowRunId,
        activeOrgQueryKeyScope,
      ),
      refetchInterval: (query: { state: WatchedRunState }) =>
        getWatchedRunRefetchInterval(query.state),
      enabled: !!workflowPermanentId,
    })),
  });
}

export {
  getRunStatusRefetchInterval,
  getWatchedRunRefetchInterval,
  POLL_OUTAGE_BUDGET_MS,
  RUN_STATUS_OUTAGE_RETRY_INTERVAL_MS,
  RUN_STATUS_POLL_INTERVAL_MS,
  useWorkflowRunByIdQuery,
  useWorkflowRunQueries,
  useWorkflowRunQuery,
};
