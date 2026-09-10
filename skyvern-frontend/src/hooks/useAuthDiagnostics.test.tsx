// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }));

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({ get: mockGet }),
}));

import { useAuthDiagnostics } from "./useAuthDiagnostics";

function axiosErrorWithStatus(status: number) {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    isAxiosError: true,
    response: { status },
  });
}

function renderDiagnostics() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return renderHook(() => useAuthDiagnostics(), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    ),
  });
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("useAuthDiagnostics", () => {
  it("passes through a diagnostics payload from the backend", async () => {
    mockGet.mockResolvedValue({ data: { status: "invalid" } });
    const { result } = renderDiagnostics();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ status: "invalid" });
  });

  it("treats a 403 (endpoint restricted to loopback) as ok", async () => {
    mockGet.mockRejectedValue(axiosErrorWithStatus(403));
    const { result } = renderDiagnostics();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ status: "ok" });
  });

  it("treats a 404 (endpoint not present) as ok", async () => {
    mockGet.mockRejectedValue(axiosErrorWithStatus(404));
    const { result } = renderDiagnostics();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ status: "ok" });
  });

  it("surfaces other HTTP errors", async () => {
    mockGet.mockRejectedValue(axiosErrorWithStatus(500));
    const { result } = renderDiagnostics();
    await waitFor(() => expect(result.current.isError).toBe(true));
  });

  it("surfaces non-HTTP errors", async () => {
    mockGet.mockRejectedValue(new Error("Network Error"));
    const { result } = renderDiagnostics();
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});
