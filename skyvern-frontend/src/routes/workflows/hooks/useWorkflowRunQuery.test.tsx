import { AxiosError, AxiosHeaders } from "axios";
import {
  QueryClient,
  QueryClientProvider,
  QueryObserver,
} from "@tanstack/react-query";
import { cleanup, render, renderHook, waitFor } from "@testing-library/react";
import { type ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { Status } from "@/api/types";
import { retryTransientNetworkFailures } from "@/api/QueryClient";
import { TooltipProvider } from "@/components/ui/tooltip";
import { RunPaneActions } from "@/routes/workflows/studio/runview/RunPaneHeader";
import { useExecutingBlockRun } from "@/routes/workflows/studio/useExecutingBlockRun";
import { useStudioRunSignals } from "@/routes/workflows/studio/useStudioRunSignals";

import {
  getRunStatusRefetchInterval,
  POLL_OUTAGE_BUDGET_MS,
  RUN_STATUS_OUTAGE_RETRY_INTERVAL_MS,
  RUN_STATUS_POLL_INTERVAL_MS,
  useWorkflowRunQuery,
} from "./useWorkflowRunQuery";
import { useWorkflowRunWithWorkflowQuery } from "./useWorkflowRunWithWorkflowQuery";

const { getClientMock } = vi.hoisted(() => ({ getClientMock: vi.fn() }));

vi.mock("@/api/AxiosClient", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/AxiosClient")>()),
  getClient: getClientMock,
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));
vi.mock("./useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [] }),
}));

function httpError(status: number): AxiosError {
  const error = new AxiosError("request failed");
  error.response = {
    status,
    statusText: "",
    data: null,
    headers: {},
    config: { headers: new AxiosHeaders() },
  };
  return error;
}

// Only a real observer driven through repeated failed polls distinguishes a
// per-fetch failure counter from one that accumulates across an outage.
async function outageAcrossFailedPolls(
  error: unknown,
  polls: number,
): Promise<{ gaps: number[]; intervals: Array<number | false> }> {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: retryTransientNetworkFailures } },
  });
  let succeed = true;
  const observer = new QueryObserver(client, {
    queryKey: ["run-status-poll-probe"],
    queryFn: async () => {
      if (succeed) {
        return { status: Status.Running };
      }
      throw error;
    },
  });
  const unsubscribe = observer.subscribe(() => {});
  await observer.refetch();
  succeed = false;

  const gaps: number[] = [];
  const intervals: Array<number | false> = [];
  for (let i = 0; i < polls; i++) {
    await new Promise((resolve) => setTimeout(resolve, 5));
    await observer.refetch();
    const state = client.getQueryCache().find({
      queryKey: ["run-status-poll-probe"],
    })!.state;
    gaps.push(state.errorUpdatedAt - state.dataUpdatedAt);
    intervals.push(
      getRunStatusRefetchInterval(
        state as unknown as Parameters<typeof getRunStatusRefetchInterval>[0],
      ),
    );
  }
  unsubscribe();
  client.clear();
  return { gaps, intervals };
}

describe("getRunStatusRefetchInterval", () => {
  test("keeps polling through a short outage so a run that finishes mid-outage is still observed", () => {
    const lastSuccess = 1_000_000;
    expect(
      getRunStatusRefetchInterval({
        status: "error",
        data: { status: Status.Running },
        dataUpdatedAt: lastSuccess,
        errorUpdatedAt: lastSuccess + RUN_STATUS_POLL_INTERVAL_MS,
      }),
    ).toBe(RUN_STATUS_POLL_INTERVAL_MS);
  });

  test("retries at a quieter cadence once the outage outlives its budget", () => {
    const lastSuccess = 1_000_000;
    expect(
      getRunStatusRefetchInterval({
        status: "error",
        data: { status: Status.Running },
        dataUpdatedAt: lastSuccess,
        errorUpdatedAt: lastSuccess + POLL_OUTAGE_BUDGET_MS + 1,
      }),
    ).toBe(RUN_STATUS_OUTAGE_RETRY_INTERVAL_MS);
  });

  test("does not poll before the first successful fetch", () => {
    expect(
      getRunStatusRefetchInterval({
        status: "pending",
        data: undefined,
        dataUpdatedAt: 0,
        errorUpdatedAt: 0,
      }),
    ).toBe(false);
  });

  test("polls while the run is not finalized", () => {
    const now = 1_000_000;
    expect(
      getRunStatusRefetchInterval({
        status: "success",
        data: { status: Status.Running },
        dataUpdatedAt: now,
        errorUpdatedAt: 0,
      }),
    ).toBe(RUN_STATUS_POLL_INTERVAL_MS);
  });

  test("stops polling once the run is finalized", () => {
    const now = 1_000_000;
    expect(
      getRunStatusRefetchInterval({
        status: "success",
        data: { status: Status.Completed },
        dataUpdatedAt: now,
        errorUpdatedAt: 0,
      }),
    ).toBe(false);
  });

  test("the outage measure accumulates across failed polls instead of resetting per fetch", async () => {
    const { gaps } = await outageAcrossFailedPolls(httpError(500), 4);
    expect(gaps).toHaveLength(4);
    for (let i = 1; i < gaps.length; i++) {
      expect(gaps[i]).toBeGreaterThan(gaps[i - 1]!);
    }
  });

  test("a retried transport failure is not treated as an exhausted outage", async () => {
    const { intervals } = await outageAcrossFailedPolls(
      new AxiosError("Network Error", AxiosError.ERR_NETWORK),
      1,
    );
    expect(intervals[0]).toBe(RUN_STATUS_POLL_INTERVAL_MS);
  });
});

const RUN_A_ID = "wr_identity_a";
const RUN_B_ID = "wr_identity_b";

function buildRun(id: string, status: Status) {
  return {
    workflow_run_id: id,
    status,
    parameters: {},
    task_v2: null,
    browser_session_id: "pbs_1",
    workflow: { workflow_permanent_id: "wpid_1", deleted_at: null },
  };
}

const respondedRuns = new Map<string, unknown>();

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function harness(client: QueryClient, path: string) {
  return function Harness({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <TooltipProvider delayDuration={0}>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route
                path="/agents/:workflowPermanentId/studio"
                element={<>{children}</>}
              />
              <Route
                path="/agents/:workflowPermanentId/runs/:workflowRunId"
                element={<>{children}</>}
              />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    );
  };
}

// Seeds the payload a run switch leaves behind: run A's response cached under the
// key of the run now being requested, which is what keepPreviousData hands over.
function seedRetained(client: QueryClient, requestedId: string, run: unknown) {
  client.setQueryData(["workflowRun", requestedId], run);
  client.setQueryData(["workflowRun", "wpid_1", requestedId], run);
}

beforeEach(() => {
  respondedRuns.clear();
  getClientMock.mockResolvedValue({
    get: (url: string) => {
      for (const [id, run] of respondedRuns) {
        if (url.includes(id)) {
          return Promise.resolve({ data: run });
        }
      }
      return new Promise(() => {});
    },
    post: vi.fn(),
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("run identity withholding", () => {
  test("a payload retained across a run switch is withheld from both hooks", async () => {
    respondedRuns.set(RUN_A_ID, buildRun(RUN_A_ID, Status.Running));
    const client = makeClient();
    const wrapper = harness(client, "/agents/wpid_1/studio");
    const { result, rerender } = renderHook(
      ({ id }: { id: string | undefined }) => ({
        plain: useWorkflowRunQuery({ workflowRunId: id }),
        withWorkflow: useWorkflowRunWithWorkflowQuery({ workflowRunId: id }),
      }),
      { wrapper, initialProps: { id: RUN_A_ID as string | undefined } },
    );

    await waitFor(() => {
      expect(result.current.plain.data?.workflow_run_id).toBe(RUN_A_ID);
      expect(result.current.withWorkflow.data?.workflow_run_id).toBe(RUN_A_ID);
    });

    rerender({ id: RUN_B_ID });

    expect(result.current.plain.isPlaceholderData).toBe(true);
    expect(result.current.withWorkflow.isPlaceholderData).toBe(true);
    expect(result.current.plain.data).toBeUndefined();
    expect(result.current.withWorkflow.data).toBeUndefined();
    expect(result.current.plain.isError).toBe(false);
  });

  test("a caller that clears its run id gets no run, not the one named by the route", async () => {
    respondedRuns.set(RUN_A_ID, buildRun(RUN_A_ID, Status.Running));
    const client = makeClient();
    const wrapper = harness(client, `/agents/wpid_1/runs/${RUN_A_ID}`);
    const { result, rerender } = renderHook(
      ({ id }: { id: string | undefined }) => ({
        plain: useWorkflowRunQuery({ workflowRunId: id }),
        withWorkflow: useWorkflowRunWithWorkflowQuery({ workflowRunId: id }),
      }),
      { wrapper, initialProps: { id: RUN_A_ID as string | undefined } },
    );

    await waitFor(() =>
      expect(result.current.withWorkflow.data?.workflow_run_id).toBe(RUN_A_ID),
    );

    rerender({ id: undefined });

    expect(result.current.plain.data).toBeUndefined();
    expect(result.current.withWorkflow.data).toBeUndefined();
    // Proves run A's payload is still retained and merely withheld, rather than
    // the cache having been emptied by the rerender.
    expect(result.current.plain.isPlaceholderData).toBe(true);
    expect(result.current.withWorkflow.isPlaceholderData).toBe(true);
  });

  test("an omitted run id still defers to the route", async () => {
    respondedRuns.set(RUN_A_ID, buildRun(RUN_A_ID, Status.Running));
    const client = makeClient();
    const { result } = renderHook(() => useWorkflowRunWithWorkflowQuery(), {
      wrapper: harness(client, `/agents/wpid_1/runs/${RUN_A_ID}`),
    });

    await waitFor(() =>
      expect(result.current.data?.workflow_run_id).toBe(RUN_A_ID),
    );
  });

  test("a matching payload still reaches the caller", async () => {
    respondedRuns.set(RUN_B_ID, buildRun(RUN_B_ID, Status.Running));
    const client = makeClient();
    const { result } = renderHook(
      () => useWorkflowRunWithWorkflowQuery({ workflowRunId: RUN_B_ID }),
      { wrapper: harness(client, "/agents/wpid_1/studio") },
    );

    await waitFor(() =>
      expect(result.current.data?.workflow_run_id).toBe(RUN_B_ID),
    );
  });
});

describe("studio consumers of a retained run payload", () => {
  test("the run-tab label and status dot describe no run", () => {
    const client = makeClient();
    seedRetained(client, RUN_B_ID, buildRun(RUN_A_ID, Status.Completed));
    const { result } = renderHook(() => useStudioRunSignals(), {
      wrapper: harness(client, `/agents/wpid_1/studio?wr=${RUN_B_ID}`),
    });

    expect(result.current.runId).toBe(RUN_B_ID);
    expect(result.current.runStatus).toBeNull();
  });

  test("no block run is reported as executing", () => {
    const client = makeClient();
    seedRetained(client, RUN_B_ID, buildRun(RUN_A_ID, Status.Running));
    client.setQueryData(["debugSession", "wpid_1"], {
      browser_session_id: "pbs_1",
    });
    const { result } = renderHook(() => useExecutingBlockRun(), {
      wrapper: harness(
        client,
        `/agents/wpid_1/studio?wr=${RUN_B_ID}&bl=Block%201`,
      ),
    });

    expect(result.current).toBe(false);
  });

  test("the run pane's actions menu renders nothing", () => {
    const client = makeClient();
    seedRetained(client, RUN_B_ID, buildRun(RUN_A_ID, Status.Completed));
    const Harness = harness(client, `/agents/wpid_1/studio?wr=${RUN_B_ID}`);
    const { container } = render(
      <Harness>
        <RunPaneActions />
      </Harness>,
    );

    expect(container.textContent).toBe("");
  });
});
