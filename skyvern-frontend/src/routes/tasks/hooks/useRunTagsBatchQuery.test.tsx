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

import { useRunTagsBatchQuery } from "./useRunTagsBatchQuery";

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

describe("useRunTagsBatchQuery", () => {
  it("fetches run tags from the run batch endpoint with sorted ids", async () => {
    mockGet.mockResolvedValue({
      data: {
        run_tags: {
          wr_1: [{ key: "env", value: "prod" }],
          wr_2: [],
        },
      },
    });

    const { result } = renderHook(
      () => useRunTagsBatchQuery(["wr_2", "wr_1"]),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockGet).toHaveBeenCalledWith("/run-tags", expect.anything());
    expect(lastParams().get("workflow_run_ids")).toBe("wr_1,wr_2");
    expect(result.current.data).toEqual({
      wr_1: [{ key: "env", value: "prod" }],
      wr_2: [],
    });
  });
});
