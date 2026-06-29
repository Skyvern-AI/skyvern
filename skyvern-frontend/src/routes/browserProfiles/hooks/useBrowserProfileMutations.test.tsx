// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PINNED_RESIDENTIAL_ISP_PROXY_LOCATION } from "@/api/types";

const { mockPatch } = vi.hoisted(() => ({ mockPatch: vi.fn() }));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ patch: mockPatch }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

import { useUpdateBrowserProfileMutation } from "./useBrowserProfileMutations";

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("useUpdateBrowserProfileMutation proxy pin payloads", () => {
  it("omits proxy_session_id when keeping the existing consistent IP identity", async () => {
    mockPatch.mockResolvedValue({ data: {} });
    const { result } = renderHook(() => useUpdateBrowserProfileMutation(), {
      wrapper,
    });

    act(() => {
      result.current.mutate({
        profileId: "bp_123",
        proxy_location: PINNED_RESIDENTIAL_ISP_PROXY_LOCATION,
        proxy_session_id: undefined,
      });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockPatch).toHaveBeenCalledWith("/browser_profiles/bp_123", {
      proxy_location: PINNED_RESIDENTIAL_ISP_PROXY_LOCATION,
    });
  });

  it("sends rotate_proxy_session_id when rotating the consistent IP identity", async () => {
    mockPatch.mockResolvedValue({ data: {} });
    const { result } = renderHook(() => useUpdateBrowserProfileMutation(), {
      wrapper,
    });

    act(() => {
      result.current.mutate({
        profileId: "bp_123",
        proxy_location: PINNED_RESIDENTIAL_ISP_PROXY_LOCATION,
        rotate_proxy_session_id: true,
      });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockPatch).toHaveBeenCalledWith("/browser_profiles/bp_123", {
      proxy_location: PINNED_RESIDENTIAL_ISP_PROXY_LOCATION,
      rotate_proxy_session_id: true,
    });
  });

  it("sends null proxy fields when disabling consistent IP", async () => {
    mockPatch.mockResolvedValue({ data: {} });
    const { result } = renderHook(() => useUpdateBrowserProfileMutation(), {
      wrapper,
    });

    act(() => {
      result.current.mutate({
        profileId: "bp_123",
        proxy_location: null,
        proxy_session_id: null,
      });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockPatch).toHaveBeenCalledWith("/browser_profiles/bp_123", {
      proxy_location: null,
      proxy_session_id: null,
    });
  });
});
