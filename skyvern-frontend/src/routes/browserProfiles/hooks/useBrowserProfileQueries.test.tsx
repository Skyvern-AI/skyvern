// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ get: mockGet }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));

import { useBrowserProfileQuery } from "./useBrowserProfileQuery";
import { useBrowserProfileUsageQuery } from "./useBrowserProfileUsageQuery";

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      {children}
    </QueryClientProvider>
  );
}

afterEach(() => vi.clearAllMocks());

describe("browser profile queries", () => {
  it("does not fetch a missing profile even when callers enable the query", async () => {
    const { result } = renderHook(
      () => useBrowserProfileQuery(undefined, { enabled: true }),
      { wrapper },
    );

    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));

    expect(result.current.fetchStatus).toBe("idle");
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("does not fetch usage for a missing profile even when callers enable the query", async () => {
    const { result } = renderHook(
      () => useBrowserProfileUsageQuery(undefined, { enabled: true }),
      { wrapper },
    );

    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));

    expect(result.current.fetchStatus).toBe("idle");
    expect(mockGet).not.toHaveBeenCalled();
  });
});
