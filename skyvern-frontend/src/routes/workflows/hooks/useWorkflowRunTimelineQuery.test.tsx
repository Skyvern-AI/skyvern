// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockGet, mockGetClient } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockGetClient: vi.fn(),
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
      status: "completed",
      workflow: { workflow_permanent_id: "wpid_1" },
    },
    dataUpdatedAt: 0,
  }),
}));

import { useWorkflowRunTimelineQuery } from "./useWorkflowRunTimelineQuery";

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
});

describe("useWorkflowRunTimelineQuery", () => {
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
});
