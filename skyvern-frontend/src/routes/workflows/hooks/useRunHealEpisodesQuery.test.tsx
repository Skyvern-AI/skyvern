// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockGet, mockGetClient, runState } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockGetClient: vi.fn(),
  runState: { status: "running" },
}));

vi.mock("@/api/AxiosClient", () => ({ getClient: mockGetClient }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("./useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: {
      status: runState.status,
      workflow: { workflow_permanent_id: "wpid_1" },
    },
    dataUpdatedAt: 0,
  }),
}));

import { useRunHealEpisodesQuery } from "./useRunHealEpisodesQuery";

afterEach(() => {
  vi.clearAllMocks();
  runState.status = "running";
});

describe("useRunHealEpisodesQuery", () => {
  // Episodes recorded after the reader unmounts are only picked up by the invalidation that
  // follows a run-query poll, and a finalized run stops polling. Without the status in the key a
  // reader that leaves mid-run and returns after the run finished is served the episodes as of
  // the moment it left, for as long as that entry survives garbage collection.
  it("re-reads episodes when a reader returns after the run finalized", async () => {
    mockGet.mockResolvedValue({ data: { episodes: [] } });
    mockGetClient.mockResolvedValue({ get: mockGet });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    function wrapper({ children }: { children: ReactNode }) {
      return (
        <QueryClientProvider client={queryClient}>
          {children}
        </QueryClientProvider>
      );
    }

    const midRun = renderHook(
      () => useRunHealEpisodesQuery({ workflowRunId: "wr_1" }),
      { wrapper },
    );
    await waitFor(() => expect(midRun.result.current.isSuccess).toBe(true));
    const midRunReads = mockGet.mock.calls.length;
    midRun.unmount();

    runState.status = "completed";
    const afterRun = renderHook(
      () => useRunHealEpisodesQuery({ workflowRunId: "wr_1" }),
      { wrapper },
    );

    await waitFor(() => expect(afterRun.result.current.isSuccess).toBe(true));
    expect(mockGet.mock.calls.length).toBeGreaterThan(midRunReads);
  });

  // The path the readers actually take when a run finishes under them: no remount, just the status
  // swap onto an uncached key. RunHealChip and BlockHealPanel both render null on absent data, so
  // dropping the previous episodes here blinks the chip and the panel out right at completion.
  it("keeps episodes on screen while a mounted run finalizes", async () => {
    const episodes = {
      episodes: [{ workflow_run_block_id: "wrb_1" }],
      summary: { blocks_with_heal_attempt: 1 },
    };
    mockGet.mockResolvedValue({ data: episodes });
    mockGetClient.mockResolvedValue({ get: mockGet });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    function wrapper({ children }: { children: ReactNode }) {
      return (
        <QueryClientProvider client={queryClient}>
          {children}
        </QueryClientProvider>
      );
    }

    const { result, rerender } = renderHook(
      () => useRunHealEpisodesQuery({ workflowRunId: "wr_1" }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const readsWhileLive = mockGet.mock.calls.length;

    runState.status = "completed";
    rerender();

    expect(result.current.data).toEqual(episodes);
    await waitFor(() =>
      expect(mockGet.mock.calls.length).toBeGreaterThan(readsWhileLive),
    );
  });
});
