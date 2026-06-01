// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getClient } from "@/api/AxiosClient";
import { useCredentialsQuery } from "./useCredentialsQuery";

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const mockedGetClient = vi.mocked(getClient);

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
  };
}

function stubClient() {
  const getMock = vi.fn().mockResolvedValue({ data: [] });
  mockedGetClient.mockResolvedValue({
    get: getMock,
  } as unknown as Awaited<ReturnType<typeof getClient>>);
  return getMock;
}

function lastParams(getMock: ReturnType<typeof vi.fn>): URLSearchParams {
  const call = getMock.mock.calls[0];
  if (!call) {
    throw new Error("client.get was not called");
  }
  return (call[1] as { params: URLSearchParams }).params;
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("useCredentialsQuery — search + type query params (SKY-5679)", () => {
  it("sends search and credential_type when provided", async () => {
    const getMock = stubClient();

    renderHook(
      () =>
        useCredentialsQuery({ search: "ohio", credential_type: "password" }),
      { wrapper: makeWrapper() },
    );

    await waitFor(() => expect(getMock).toHaveBeenCalled());
    expect(getMock).toHaveBeenCalledWith("/credentials", expect.anything());
    const params = lastParams(getMock);
    expect(params.get("search")).toBe("ohio");
    expect(params.get("credential_type")).toBe("password");
  });

  it("omits search and credential_type when they are not provided", async () => {
    const getMock = stubClient();

    renderHook(() => useCredentialsQuery(), { wrapper: makeWrapper() });

    await waitFor(() => expect(getMock).toHaveBeenCalled());
    const params = lastParams(getMock);
    expect(params.has("search")).toBe(false);
    expect(params.has("credential_type")).toBe(false);
    // pagination params are always present
    expect(params.get("page")).toBe("1");
    expect(params.get("page_size")).toBe("25");
  });
});
