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

import { useRunTagsQuery } from "./useRunTagsQuery";

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

describe("useRunTagsQuery", () => {
  it("fetches current tags for one workflow run", async () => {
    mockGet.mockResolvedValue({
      data: {
        workflow_run_id: "wr_1",
        tags: [{ key: null, value: "manual", source: "manual" }],
      },
    });

    const { result } = renderHook(() => useRunTagsQuery("wr_1"), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockGet).toHaveBeenCalledWith("/runs/wr_1/tags");
    expect(result.current.data).toEqual([{ key: null, value: "manual" }]);
  });
});
