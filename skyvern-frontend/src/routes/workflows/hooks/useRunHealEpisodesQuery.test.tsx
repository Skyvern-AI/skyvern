// @vitest-environment jsdom

import type { ReactNode } from "react";
import {
  focusManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockGet, mockGetClient, runState } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockGetClient: vi.fn(),
  runState: {
    status: "running",
    retry_pending: false,
    attempt: 1,
    dataUpdatedAt: 0,
  },
}));

vi.mock("@/api/AxiosClient", () => ({ getClient: mockGetClient }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("./useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: {
      status: runState.status,
      attempt: runState.attempt,
      retry_pending: runState.retry_pending,
      workflow: { workflow_permanent_id: "wpid_1" },
    },
    dataUpdatedAt: runState.dataUpdatedAt,
  }),
}));

import { useRunHealEpisodesQuery } from "./useRunHealEpisodesQuery";

afterEach(() => {
  vi.clearAllMocks();
  runState.status = "running";
  runState.retry_pending = false;
  runState.attempt = 1;
  runState.dataUpdatedAt = 0;
  focusManager.setFocused(undefined);
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

it("refreshes execution updates but retains episodes through retry-wait polls, focus, and remount", async () => {
  const initial = { episodes: [{ workflow_run_block_id: "wrb_initial" }] };
  const duringExecution = {
    episodes: [{ workflow_run_block_id: "wrb_executing" }],
  };
  const finished = { episodes: [{ workflow_run_block_id: "wrb_finished" }] };
  const nextAttempt = { episodes: [{ workflow_run_block_id: "wrb_next" }] };
  let serverData = initial;
  mockGet.mockImplementation(async () => ({ data: serverData }));
  mockGetClient.mockResolvedValue({ get: mockGet });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
  }
  const useEpisodes = () => useRunHealEpisodesQuery({ workflowRunId: "wr_1" });
  const mounted = renderHook(useEpisodes, { wrapper });
  await waitFor(() => expect(mounted.result.current.data).toEqual(initial));

  serverData = duringExecution;
  runState.dataUpdatedAt++;
  mounted.rerender();
  await waitFor(() =>
    expect(mounted.result.current.data).toEqual(duringExecution),
  );

  serverData = finished;
  runState.status = "failed";
  runState.retry_pending = true;
  runState.dataUpdatedAt++;
  mounted.rerender();
  await waitFor(() => expect(mounted.result.current.data).toEqual(finished));

  serverData = nextAttempt;
  for (let tick = 0; tick < 3; tick++) {
    await act(async () => {
      runState.dataUpdatedAt++;
      mounted.rerender();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(mounted.result.current.data).toEqual(finished);
    expect(
      queryClient.getQueryState([
        "run-heal-episodes",
        "wr_1",
        "failed",
        1,
        true,
      ])?.isInvalidated,
    ).toBe(false);
  }
  await act(async () => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  expect(mounted.result.current.data).toEqual(finished);
  mounted.unmount();
  const remounted = renderHook(useEpisodes, { wrapper });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  expect(remounted.result.current.data).toEqual(finished);

  runState.status = "running";
  runState.retry_pending = false;
  runState.dataUpdatedAt++;
  remounted.rerender();
  await waitFor(() =>
    expect(remounted.result.current.data).toEqual(nextAttempt),
  );
  remounted.unmount();
  queryClient.clear();
});

it("re-reads consecutive failed attempts without showing the previous attempt's episodes", async () => {
  runState.status = "failed";
  const first = { episodes: [{ workflow_run_block_id: "wrb_attempt_1" }] };
  const second = { episodes: [{ workflow_run_block_id: "wrb_attempt_2" }] };
  mockGet.mockResolvedValue({ data: first });
  mockGetClient.mockResolvedValue({ get: mockGet });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result, rerender, unmount } = renderHook(
    () => useRunHealEpisodesQuery({ workflowRunId: "wr_fail_twice" }),
    { wrapper },
  );
  await waitFor(() => expect(result.current.data).toEqual(first));
  let finish: (value: { data: typeof second }) => void = () => {};
  mockGet.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  runState.attempt = 2;
  rerender();
  await waitFor(() => expect(result.current.isFetching).toBe(true));
  expect(result.current.data).toBeUndefined();
  await act(async () => finish({ data: second }));
  await waitFor(() => expect(result.current.data).toEqual(second));
  unmount();
  client.clear();
});
