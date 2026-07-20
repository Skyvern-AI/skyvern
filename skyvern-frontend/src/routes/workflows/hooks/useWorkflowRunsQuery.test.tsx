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
vi.mock("./useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [] }),
}));

import { useWorkflowRunsQuery } from "./useWorkflowRunsQuery";

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

describe("useWorkflowRunsQuery", () => {
  it("keeps the legacy route when no run-tag filter is active", async () => {
    mockGet.mockResolvedValue({ data: [] });
    mockGetClient.mockResolvedValue({ get: mockGet });

    renderHook(
      () =>
        useWorkflowRunsQuery({
          workflowPermanentId: "wpid_1",
          page: 1,
        }),
      { wrapper },
    );

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(mockGetClient).toHaveBeenCalledWith(null);
  });

  it("keeps the legacy route (child runs visible) when filtering by run tags", async () => {
    mockGet.mockResolvedValue({ data: [] });
    mockGetClient.mockResolvedValue({ get: mockGet });

    renderHook(
      () =>
        useWorkflowRunsQuery({
          workflowPermanentId: "wpid_1",
          page: 1,
          tags: "env:prod",
        }),
      { wrapper },
    );

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(mockGetClient).toHaveBeenCalledWith(null);
    expect(mockGet).toHaveBeenCalledWith(
      "/workflows/wpid_1/runs",
      expect.anything(),
    );
    const params = mockGet.mock.calls[0]?.[1].params as URLSearchParams;
    expect(params.get("tags")).toBe("env:prod");
  });
});
