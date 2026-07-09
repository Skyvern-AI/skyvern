// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ get: mockGet }),
}));

import { useTagValuesListQuery, useTagValuesQuery } from "./useTagValuesQuery";

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function lastParams(): URLSearchParams {
  const call = mockGet.mock.calls[0];
  if (!call) {
    throw new Error("client.get was not called");
  }
  return (call[1] as { params: URLSearchParams }).params;
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("useTagValuesQuery", () => {
  it("passes key when fetching the color map for a specific tag group", async () => {
    mockGet.mockResolvedValue({
      data: [{ key: "env", value: "prod", color: "green" }],
    });

    renderHook(() => useTagValuesQuery({ key: "env" }), { wrapper });

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(mockGet).toHaveBeenCalledWith("/tag-values", expect.anything());
    expect(lastParams().get("key")).toBe("env");
  });
});

describe("useTagValuesListQuery", () => {
  it("passes key when fetching raw values for a specific tag group", async () => {
    mockGet.mockResolvedValue({
      data: [{ key: "team", value: "ops", color: "blue" }],
    });

    renderHook(() => useTagValuesListQuery({ key: "team" }), { wrapper });

    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(mockGet).toHaveBeenCalledWith("/tag-values", expect.anything());
    expect(lastParams().get("key")).toBe("team");
  });
});
