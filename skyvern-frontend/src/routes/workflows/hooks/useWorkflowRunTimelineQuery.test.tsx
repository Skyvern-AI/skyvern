// @vitest-environment jsdom

import type { ReactNode } from "react";
import {
  focusManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockGet, mockGetClient, runState } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockGetClient: vi.fn(),
  runState: { status: "completed", attempt: 1, retry_pending: false },
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: mockGetClient,
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/hooks/useFirstParam", () => ({
  useFirstParam: () => "wr_1",
}));
vi.mock("./useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [] }),
}));
vi.mock("./useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: {
      ...runState,
      workflow: { workflow_permanent_id: "wpid_1" },
    },
    dataUpdatedAt: 0,
  }),
}));

import {
  RUNNING_TIMELINE_REFETCH_INTERVAL_MS,
  useWorkflowRunTimelineQuery,
} from "./useWorkflowRunTimelineQuery";

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
  focusManager.setFocused(undefined);
  runState.status = "completed";
  runState.attempt = 1;
  runState.retry_pending = false;
});

describe("useWorkflowRunTimelineQuery", () => {
  it("stops polling during a retry delay and fetches the next attempt", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    focusManager.setFocused(true);
    runState.status = "failed";
    runState.retry_pending = true;
    mockGet.mockResolvedValue({ data: [] });
    mockGetClient.mockResolvedValue({ get: mockGet });

    const { result, rerender } = renderHook(
      () => useWorkflowRunTimelineQuery(),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const readsBeforeDelay = mockGet.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3 * RUNNING_TIMELINE_REFETCH_INTERVAL_MS);
    expect(mockGet).toHaveBeenCalledTimes(readsBeforeDelay);

    runState.status = "queued";
    runState.attempt = 2;
    runState.retry_pending = false;
    rerender();
    await waitFor(() =>
      expect(mockGet.mock.calls.length).toBeGreaterThan(readsBeforeDelay),
    );

    runState.status = "running";
    rerender();
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    const readsBeforePolling = mockGet.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3 * RUNNING_TIMELINE_REFETCH_INTERVAL_MS);
    expect(mockGet.mock.calls.length).toBeGreaterThan(readsBeforePolling);
  });

  it("returns the timeline when the response is an array", async () => {
    mockGet.mockResolvedValue({ data: [] });
    mockGetClient.mockResolvedValue({ get: mockGet });

    const { result } = renderHook(() => useWorkflowRunTimelineQuery(), {
      wrapper,
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
  });

  // A 2xx whose body will not JSON.parse arrives as a raw string, and an object
  // body arrives as an object; either reaches callers typed as an array (SKY-13162).
  it.each([
    ["a truncated JSON array", '[{"type":"block"'],
    ["an HTML error page", "<!doctype html><html></html>"],
    ["an empty body", ""],
    ["a JSON object", { detail: "nope" }],
  ])("fails the query when the response is %s", async (_label, body) => {
    mockGet.mockResolvedValue({ data: body });
    mockGetClient.mockResolvedValue({ get: mockGet });

    const { result } = renderHook(() => useWorkflowRunTimelineQuery(), {
      wrapper,
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    // Callers guard on `undefined`; a non-array must never reach them.
    expect(result.current.data).toBeUndefined();
  });

  // The poll cannot be the thing that reads a run's last blocks: the run-status query can report
  // the terminal state between two timeline ticks, which cancels the timer before it fires again.
  // The reader stays mounted through that, so no mount or focus refetch covers it either, and the
  // timeline keeps whatever partial snapshot it last saw — rendered as blocks that "did not
  // execute", or as empty when the run had not written its first block yet.
  it.each([
    ["nothing yet", []],
    ["a partial timeline", [{ type: "block" }]],
  ])(
    "re-reads when a mounted run finalizes having read %s",
    async (_label, body) => {
      runState.status = "running";
      mockGet.mockResolvedValue({ data: body });
      mockGetClient.mockResolvedValue({ get: mockGet });

      const { result, rerender } = renderHook(
        () => useWorkflowRunTimelineQuery(),
        { wrapper },
      );
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      const readsWhileLive = mockGet.mock.calls.length;

      runState.status = "completed";
      rerender();

      // The swap lands on a key with nothing cached, so without keepPreviousData every reader
      // gating on isLoading would flash a skeleton the moment a run completes.
      expect(result.current.data).toEqual(body);
      expect(result.current.isLoading).toBe(false);

      await waitFor(() =>
        expect(mockGet.mock.calls.length).toBeGreaterThan(readsWhileLive),
      );
    },
  );

  // Background polling exists so a run watched from another window still fills in its rows. Only a
  // running run writes any, and a paused one waits on a human — polling through that would turn a
  // tab left open on a paused run into hours of requests it never made before.
  it.each([
    ["polls through a running run", "running", true],
    ["stays quiet through a paused run", "paused", false],
  ])("a backgrounded tab %s", async (_label, status, expectedToPoll) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    runState.status = status;
    mockGet.mockResolvedValue({ data: [] });
    mockGetClient.mockResolvedValue({ get: mockGet });

    const { result } = renderHook(() => useWorkflowRunTimelineQuery(), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    focusManager.setFocused(false);
    const readsWhileFocused = mockGet.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3 * RUNNING_TIMELINE_REFETCH_INTERVAL_MS);

    expect(mockGet.mock.calls.length > readsWhileFocused).toBe(expectedToPoll);
  });
});
