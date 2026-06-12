// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getClient } from "@/api/AxiosClient";
import { useCredentialTestStore } from "@/store/useCredentialTestStore";
import { useBackgroundCredentialTest } from "./useBackgroundCredentialTest";

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

const mockedGetClient = vi.mocked(getClient);

function makeTest(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    credentialId: "cred_1",
    workflowRunId: "wr_1",
    url: "https://example.com/login",
    startTime: Date.now() - 1000,
    ...overrides,
  };
}

afterEach(() => {
  useCredentialTestStore.getState().clearActiveTest();
  localStorage.clear();
  vi.clearAllMocks();
});

describe("useBackgroundCredentialTest adoption (SKY-10855)", () => {
  function setup() {
    const queryClient = new QueryClient();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const hook = renderHook(() => useBackgroundCredentialTest(), { wrapper });
    return { hook, invalidateSpy };
  }

  it("starts polling when a test appears in the store after mount (started by another tab)", async () => {
    const get = vi.fn().mockResolvedValue({
      data: {
        status: "completed",
        browser_profile_id: "bp_1",
        tested_url: "https://example.com/login",
      },
    });
    mockedGetClient.mockResolvedValue({ get } as never);

    const { invalidateSpy } = setup();
    act(() => {
      useCredentialTestStore.getState().setActiveTest(makeTest());
    });

    await waitFor(() => {
      expect(useCredentialTestStore.getState().activeTest).toBeNull();
    });
    expect(get).toHaveBeenCalledWith("/credentials/cred_1/test/wr_1");
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["credentials"] });
  });

  it("clears an expired persisted test instead of polling it", async () => {
    const get = vi.fn();
    mockedGetClient.mockResolvedValue({ get } as never);

    setup();
    act(() => {
      useCredentialTestStore
        .getState()
        .setActiveTest(makeTest({ startTime: Date.now() - 11 * 60 * 1000 }));
    });

    await waitFor(() => {
      expect(useCredentialTestStore.getState().activeTest).toBeNull();
    });
    expect(get).not.toHaveBeenCalled();
  });
});
