// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockGet, mockGetClient, taskState } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockGetClient: vi.fn(),
  taskState: { status: "running" },
}));

vi.mock("@/api/AxiosClient", () => ({ getClient: mockGetClient }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

import { useActions } from "./useActions";

const TASK_ID = "task_1";

function actionsReadCount() {
  return mockGet.mock.calls.filter(
    ([url]) => url === `/tasks/${TASK_ID}/actions`,
  ).length;
}

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
  taskState.status = "running";
});

describe("useActions", () => {
  // The actions poll is cancelled the instant the task query ticks to a terminal status, and the
  // reader stays mounted through that. Without the task status in the actions query key nothing
  // re-reads, so the last actions the task wrote as it finished are never fetched.
  it("re-reads actions when a mounted task finalizes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockGet.mockImplementation((url: string) => {
      if (url === `/tasks/${TASK_ID}`) {
        return Promise.resolve({
          data: {
            task_id: TASK_ID,
            status: taskState.status,
            created_at: "2026-01-01T00:00:00Z",
          },
        });
      }
      return Promise.resolve({ data: [] });
    });
    mockGetClient.mockResolvedValue({ get: mockGet });

    const { result } = renderHook(() => useActions({ id: TASK_ID }), {
      wrapper,
    });
    await waitFor(() => expect(actionsReadCount()).toBeGreaterThan(0));
    const readsWhileLive = actionsReadCount();

    taskState.status = "completed";
    await vi.advanceTimersByTimeAsync(5000);

    await waitFor(() =>
      expect(actionsReadCount()).toBeGreaterThan(readsWhileLive),
    );
    expect(result.current.isLoading).toBe(false);
  });
});
