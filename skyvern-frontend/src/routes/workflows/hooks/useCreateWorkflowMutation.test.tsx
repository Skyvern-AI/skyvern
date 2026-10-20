// @vitest-environment jsdom
import type { ReactNode } from "react";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const {
  mockNavigate,
  mockPost,
  mockAgentCreationFailed,
  mockAgentCreationSucceeded,
} = vi.hoisted(() => ({
  mockNavigate: vi.fn(),
  mockPost: vi.fn(),
  mockAgentCreationFailed: vi.fn(),
  mockAgentCreationSucceeded: vi.fn(),
}));

vi.mock("react-router-dom", async () => {
  const actual =
    await vi.importActual<typeof import("react-router-dom")>(
      "react-router-dom",
    );
  return { ...actual, useNavigate: () => mockNavigate };
});

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

vi.mock("@/util/homeTelemetry", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/util/homeTelemetry")>();
  return {
    ...actual,
    HomeTelemetry: {
      ...actual.HomeTelemetry,
      agentCreationFailed: mockAgentCreationFailed,
      agentCreationSucceeded: mockAgentCreationSucceeded,
    },
  };
});

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
  it("sends raw YAML to the legacy workflow endpoint", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_x",
        workflow_definition: { blocks: [] },
      },
    });
    const { result } = renderHook(() => useCreateWorkflowMutation(), {
      wrapper,
    });

    act(() => {
      result.current.mutate({
        title: "New Agent",
        workflow_definition: { version: 2, blocks: [], parameters: [] },
      } as never);
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockPost).toHaveBeenCalledWith(
      "/workflows",
      expect.stringContaining("title: New Agent"),
      { headers: { "Content-Type": "text/plain" } },
    );
  });

  it("emits onboarding flow_completed when the creation came from the onboarding template path", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_x",
        workflow_definition: { blocks: [] },
      },
    });
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
    expect(OnboardingTelemetry.flowCompleted).toHaveBeenCalledWith("discover");
  });

  it("does not emit flow_completed for non-onboarding creations", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_y",
        workflow_definition: { blocks: [] },
      },
    });
    const { result } = renderHook(() => useCreateWorkflowMutation(), {
      wrapper,
    });

    act(() => {
      result.current.mutate({ title: "Test", _via: "sidebar" } as never);
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(OnboardingTelemetry.flowCompleted).not.toHaveBeenCalled();
  });

  it("reports blank-agent creation success after the workflow exists", async () => {
    const attempt = {
      attemptId: "attempt-blank",
      source: "blank" as const,
      handoff: false,
      variant: "revamp" as const,
    };
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_blank",
        workflow_definition: { blocks: [] },
      },
    });
    const { result } = renderHook(() => useCreateWorkflowMutation(), {
      wrapper,
    });

    act(() => {
      result.current.mutate({
        title: "New Agent",
        _agentCreationAttempt: attempt,
      } as never);
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockAgentCreationSucceeded).toHaveBeenCalledWith(
      attempt,
      "wpid_blank",
    );
    expect(mockPost.mock.calls[0]?.[1]).not.toContain("attempt-blank");
  });

  it("reports blank-agent creation failure without losing the attempt", async () => {
    const attempt = {
      attemptId: "attempt-blank",
      source: "blank" as const,
      handoff: false,
      variant: "legacy" as const,
    };
    const error = {
      isAxiosError: true,
      response: { status: 503, data: { detail: "sensitive detail" } },
    };
    mockPost.mockRejectedValue(error);
    const { result } = renderHook(() => useCreateWorkflowMutation(), {
      wrapper,
    });

    act(() => {
      result.current.mutate({
        title: "New Agent",
        _agentCreationAttempt: attempt,
      } as never);
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(mockAgentCreationFailed).toHaveBeenCalledWith(attempt, error);
  });

  it("rejects a malformed success response before recording or navigating", async () => {
    const attempt = {
      attemptId: "attempt-malformed",
      source: "blank" as const,
      handoff: false,
      variant: "revamp" as const,
    };
    mockPost.mockResolvedValue({ data: "<html>Sign in to continue</html>" });
    const { result } = renderHook(() => useCreateWorkflowMutation(), {
      wrapper,
    });

    act(() => {
      result.current.mutate({
        title: "New Agent",
        _agentCreationAttempt: attempt,
      } as never);
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(mockAgentCreationFailed).toHaveBeenCalledWith(
      attempt,
      expect.any(Error),
    );
    expect(mockAgentCreationSucceeded).not.toHaveBeenCalled();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("notifies the caller before navigating after a successful blank creation", async () => {
    mockPost.mockResolvedValue({
      data: {
        workflow_permanent_id: "wpid_blank",
        workflow_definition: { blocks: [] },
      },
    });
    const onCreated = vi.fn();
    const { result } = renderHook(
      () => useCreateWorkflowMutation({ onCreated }),
      { wrapper },
    );

    act(() => {
      result.current.mutate({ title: "New Agent", _via: "blank" } as never);
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(onCreated).toHaveBeenCalledOnce();
    expect(mockNavigate).toHaveBeenCalledWith(
      "/agents/wpid_blank/studio?via=blank",
    );
    expect(onCreated.mock.invocationCallOrder[0]).toBeLessThan(
      mockNavigate.mock.invocationCallOrder[0]!,
    );
  });

  it("does not complete onboarding when blank creation fails", async () => {
    mockPost.mockRejectedValue(new Error("create failed"));
    const onCreated = vi.fn();
    const { result } = renderHook(
      () => useCreateWorkflowMutation({ onCreated }),
      { wrapper },
    );

    act(() => {
      result.current.mutate({ title: "New Agent", _via: "blank" } as never);
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(onCreated).not.toHaveBeenCalled();
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
