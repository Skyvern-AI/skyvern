// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockPost, mockToast } = vi.hoisted(() => ({
  mockPost: vi.fn(),
  mockToast: vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ post: mockPost, patch: vi.fn() }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: mockToast }));

import { useBackgroundBrowserProfileCreate } from "./useBackgroundBrowserProfileCreate";
import { useBrowserProfileCreateStore } from "@/store/useBrowserProfileCreateStore";

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
  useBrowserProfileCreateStore.setState({ active: null });
});

describe("useBackgroundBrowserProfileCreate concurrency guard", () => {
  it("does not clobber an in-flight create for another session", async () => {
    const { result } = renderHook(() => useBackgroundBrowserProfileCreate(), {
      wrapper,
    });

    await act(async () => {
      await result.current.startBackgroundCreate({
        browserSessionId: "pbs_first",
        name: "first",
        isSessionRunning: false,
      });
    });

    expect(
      useBrowserProfileCreateStore.getState().active?.browserSessionId,
    ).toBe("pbs_first");
    mockToast.mockClear();

    await act(async () => {
      await result.current.startBackgroundCreate({
        browserSessionId: "pbs_second",
        name: "second",
        isSessionRunning: false,
      });
    });

    // The first session's in-flight create must survive — starting a second
    // session's create must fail loudly (toast) instead of silently dropping it.
    expect(
      useBrowserProfileCreateStore.getState().active?.browserSessionId,
    ).toBe("pbs_first");
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: "destructive" }),
    );
  });

  it("allows a new create once the previous one has gone stale", async () => {
    useBrowserProfileCreateStore.setState({
      active: {
        browserSessionId: "pbs_stale",
        name: "stale",
        startTime: Date.now() - 10 * 60 * 1000,
        phase: "creating",
      },
    });

    const { result } = renderHook(() => useBackgroundBrowserProfileCreate(), {
      wrapper,
    });

    await act(async () => {
      await result.current.startBackgroundCreate({
        browserSessionId: "pbs_fresh",
        name: "fresh",
        isSessionRunning: false,
      });
    });

    expect(
      useBrowserProfileCreateStore.getState().active?.browserSessionId,
    ).toBe("pbs_fresh");
  });
});
