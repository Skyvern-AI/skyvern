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

import { ActionsApiResponse, ActionTypes, Status } from "@/api/types";
import { useActions } from "./useActions";

const TASK_ID = "task_1";

// Mirrors what GET /tasks/{id}/actions puts on the wire: the route reads through
// get_task_actions_hydrated, which merges action_json, so `text` is a field this payload carries.
function actionRow(overrides: Partial<ActionsApiResponse>): ActionsApiResponse {
  return {
    action_id: "action_1",
    action_type: ActionTypes.Click,
    status: Status.Completed,
    task_id: TASK_ID,
    step_id: "step_1",
    step_order: 0,
    action_order: 0,
    confidence_float: null,
    description: null,
    reasoning: null,
    intention: null,
    response: null,
    created_by: null,
    text: null,
    ...overrides,
  };
}

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

  // A Task V3 type call records the value it typed in `text` and never writes `response`, so the
  // card's Input line came up empty on every typed field of a v3 run.
  it("reads an input_text row's typed value from text, falling back to response", async () => {
    taskState.status = "completed";
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
      if (url === `/tasks/${TASK_ID}/actions`) {
        return Promise.resolve({
          data: [
            actionRow({
              action_type: ActionTypes.InputText,
              text: "someone@example.test",
            }),
            actionRow({
              action_id: "action_2",
              response: "Clicked Contact us",
              text: null,
            }),
            actionRow({
              action_id: "action_3",
              action_type: ActionTypes.InputText,
              response: "Meridian Ave",
              text: "",
            }),
            actionRow({
              action_id: "action_4",
              action_type: ActionTypes.InputText,
              status: Status.Failed,
              response: "Timeout 30000ms exceeded waiting for locator('#zip')",
              text: "",
            }),
          ],
        });
      }
      return Promise.resolve({ data: [] });
    });
    mockGetClient.mockResolvedValue({ get: mockGet });

    const { result } = renderHook(() => useActions({ id: TASK_ID }), {
      wrapper,
    });

    await waitFor(() => expect(result.current.data).toHaveLength(4));
    expect(result.current.data[0]?.input).toBe("someone@example.test");
    // The response fallback is scoped to input_text: on every other type response is the outcome.
    expect(result.current.data[1]?.input).toBe("");
    // An empty `text` is not a typed value: a V1 row stores "" there and the answer in `response`.
    expect(result.current.data[2]?.input).toBe("Meridian Ave");
    // A recorded fill that raised also stores "" in `text`, but its `response` is the exception.
    expect(result.current.data[3]?.input).toBe("");
  });
});
