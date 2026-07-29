// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getClient } from "@/api/AxiosClient";
import CloudContext from "@/store/CloudContext";
import { useSkyvernCredentialSourceAvailable } from "./useSkyvernCredentialSourceAvailable";

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const mockedGetClient = vi.mocked(getClient);

function makeWrapper(isCloud: boolean) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <CloudContext.Provider value={isCloud}>
          {children}
        </CloudContext.Provider>
      </QueryClientProvider>
    );
  }
  return { queryClient, Wrapper };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("useSkyvernCredentialSourceAvailable", () => {
  it("is available in OSS when the credentials query succeeds", async () => {
    const get = vi.fn().mockResolvedValue({ data: [] });
    mockedGetClient.mockResolvedValue({
      get,
    } as unknown as Awaited<ReturnType<typeof getClient>>);

    const { Wrapper } = makeWrapper(false);
    const { result } = renderHook(useSkyvernCredentialSourceAvailable, {
      wrapper: Wrapper,
    });

    await waitFor(() => expect(result.current).toBe(true));
    expect(get).toHaveBeenCalledWith("/credentials", expect.anything());
  });

  it("is unavailable in OSS when the credentials query errors", async () => {
    const get = vi.fn().mockRejectedValue(new Error("unauthorized"));
    mockedGetClient.mockResolvedValue({
      get,
    } as unknown as Awaited<ReturnType<typeof getClient>>);

    const { queryClient, Wrapper } = makeWrapper(false);
    const { result } = renderHook(useSkyvernCredentialSourceAvailable, {
      wrapper: Wrapper,
    });

    await waitFor(() =>
      expect(
        queryClient
          .getQueryCache()
          .getAll()
          .find((query) => query.queryKey[0] === "credentials")?.state.status,
      ).toBe("error"),
    );
    expect(result.current).toBe(false);
  });

  it("is always available in cloud without requiring a capability probe", () => {
    const { Wrapper } = makeWrapper(true);
    const { result } = renderHook(useSkyvernCredentialSourceAvailable, {
      wrapper: Wrapper,
    });

    expect(result.current).toBe(true);
    expect(mockedGetClient).not.toHaveBeenCalled();
  });
});
