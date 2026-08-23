import { AxiosError, AxiosHeaders } from "axios";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, test } from "vitest";

import { Status } from "@/api/types";
import { retryTransientNetworkFailures } from "@/api/QueryClient";

import {
  getRunStatusRefetchInterval,
  POLL_OUTAGE_BUDGET_MS,
  RUN_STATUS_POLL_INTERVAL_MS,
} from "./useWorkflowRunQuery";

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

// The failing primitive was fetchFailureCount, which query-core resets at the
// start of every fetch, so it read 1 on every failed poll no matter how long
// the outage ran. Only a real observer driven through repeated failed polls
// distinguishes a per-fetch counter from one that accumulates across them.
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

  test("stops polling once the outage outlives its budget", () => {
    const lastSuccess = 1_000_000;
    expect(
      getRunStatusRefetchInterval({
        status: "error",
        data: { status: Status.Running },
        dataUpdatedAt: lastSuccess,
        errorUpdatedAt: lastSuccess + POLL_OUTAGE_BUDGET_MS + 1,
      }),
    ).toBe(false);
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
