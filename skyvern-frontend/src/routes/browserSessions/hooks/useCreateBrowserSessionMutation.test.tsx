// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PINNED_RESIDENTIAL_ISP_PROXY_LOCATION } from "@/api/types";

const { mockNavigate, mockPost } = vi.hoisted(() => ({
  mockNavigate: vi.fn(),
  mockPost: vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ post: mockPost }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>(
      "react-router-dom",
    );
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

import { useCreateBrowserSessionMutation } from "./useCreateBrowserSessionMutation";

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("useCreateBrowserSessionMutation proxy pin payloads", () => {
  it("sends null proxy_session_id when creating a pinned profile", async () => {
    mockPost.mockResolvedValue({
      data: { browser_session_id: "pbs_123" },
    });
    const { result } = renderHook(() => useCreateBrowserSessionMutation(), {
      wrapper,
    });

    act(() => {
      result.current.mutate({
        proxyLocation: PINNED_RESIDENTIAL_ISP_PROXY_LOCATION,
        proxySessionId: null,
        timeout: null,
        generateBrowserProfile: true,
      });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockPost).toHaveBeenCalledWith("/browser_sessions", {
      proxy_location: PINNED_RESIDENTIAL_ISP_PROXY_LOCATION,
      proxy_session_id: null,
      timeout: null,
      extensions: [],
      browser_type: null,
      generate_browser_profile: true,
    });
    expect(mockNavigate).toHaveBeenCalledWith("/browser-session/pbs_123");
  });
});
