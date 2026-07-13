// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockPost } = vi.hoisted(() => ({
  mockPost: vi.fn(),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ post: mockPost }),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

import { useApplyRunTagsMutation } from "./useRunTagMutations";

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const invalidateQueries = vi.spyOn(queryClient, "invalidateQueries");
  return {
    invalidateQueries,
    wrapper({ children }: { children: ReactNode }) {
      return (
        <QueryClientProvider client={queryClient}>
          {children}
        </QueryClientProvider>
      );
    },
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("useRunTagMutations", () => {
  it("posts tag changes to the run tag endpoint", async () => {
    mockPost.mockResolvedValue({ data: { workflow_run_id: "wr_1", tags: [] } });
    const { invalidateQueries, wrapper } = createWrapper();
    const { result } = renderHook(() => useApplyRunTagsMutation(), {
      wrapper,
    });

    act(() => {
      result.current.mutate({
        workflowRunId: "wr_1",
        data: {
          tags: [{ key: "env", value: "prod" }],
          tags_to_delete: [{ value: "old" }],
          colors: { env: "green" },
        },
      });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockPost).toHaveBeenCalledWith("/runs/wr_1/tags", {
      tags: [{ key: "env", value: "prod" }],
      tags_to_delete: [{ value: "old" }],
      colors: { env: "green" },
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["run-tags"],
    });
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["runs"] });
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ["tasks"] });
  });
});
