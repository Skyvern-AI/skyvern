// @vitest-environment jsdom
import type { ReactNode } from "react";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockPost } = vi.hoisted(() => ({ mockPost: vi.fn() }));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ post: mockPost }),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

vi.mock("@/util/onboarding/OnboardingTelemetry", () => ({
  OnboardingTelemetry: { flowCompleted: vi.fn() },
}));

import { useCreateWorkflowMutation } from "./useCreateWorkflowMutation";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";

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

describe("useCreateWorkflowMutation", () => {
  it("emits onboarding flow_completed when the creation came from the onboarding template path", async () => {
    mockPost.mockResolvedValue({ data: { workflow_permanent_id: "wpid_x" } });
    const { result } = renderHook(() => useCreateWorkflowMutation(), {
      wrapper,
    });

    act(() => {
      result.current.mutate({
        title: "Test",
        _via: "onboarding_template",
      } as never);
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(OnboardingTelemetry.flowCompleted).toHaveBeenCalledWith("dashboard");
  });

  it("does not emit flow_completed for non-onboarding creations", async () => {
    mockPost.mockResolvedValue({ data: { workflow_permanent_id: "wpid_y" } });
    const { result } = renderHook(() => useCreateWorkflowMutation(), {
      wrapper,
    });

    act(() => {
      result.current.mutate({ title: "Test", _via: "sidebar" } as never);
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(OnboardingTelemetry.flowCompleted).not.toHaveBeenCalled();
  });
});
