// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockGet, mockPost } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
}));

vi.mock("@clerk/clerk-react", () => ({
  useAuth: () => ({ isSignedIn: true }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => () => Promise.resolve("test-token"),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ get: mockGet, post: mockPost }),
}));

vi.mock("@/util/onboarding/OnboardingTelemetry", () => ({
  OnboardingTelemetry: {
    error: vi.fn(),
    registerVariant: vi.fn(),
    firstWorkflowCreated: vi.fn(),
    firstRunCompleted: vi.fn(),
  },
}));

import { OnboardingProvider } from "./OnboardingProvider";
import type { OnboardingState, OnboardingStateResponse } from "./types";
import { useOnboardingState } from "./useOnboardingState";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";

const DISMISSED_AT = "2026-01-01T00:00:00.000Z";

function freshOnboardingState(): OnboardingState {
  return {
    tour_completed_at: null,
    modal_dismissed_at: null,
    first_save_at: null,
    first_run_at: null,
    ab_variant: null,
    user_intent: null,
    seen_canvas: null,
    seen_node_adder: null,
    seen_sidebar: null,
    seen_save_run: null,
  };
}

function freshResponse(): OnboardingStateResponse {
  return {
    onboarding_state: freshOnboardingState(),
    launch_date_at_signup: "2025-01-01T00:00:00Z",
  };
}

function Consumer() {
  const { state, updateState } = useOnboardingState();
  return (
    <div>
      <span data-testid="dismissed">{String(state?.modal_dismissed_at)}</span>
      <button onClick={() => updateState({ modal_dismissed_at: DISMISSED_AT })}>
        dismiss
      </button>
    </div>
  );
}

function renderProvider() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <OnboardingProvider>
        <Consumer />
      </OnboardingProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("OnboardingProvider write resilience", () => {
  it("keeps the optimistic dismissal when the write fails", async () => {
    mockGet.mockResolvedValue({ data: freshResponse() });
    mockPost.mockRejectedValue(new Error("405 Method Not Allowed"));

    renderProvider();
    await waitFor(() =>
      expect(screen.getByTestId("dismissed").textContent).toBe("null"),
    );

    fireEvent.click(screen.getByText("dismiss"));

    // Optimistic update applies immediately.
    await waitFor(() =>
      expect(screen.getByTestId("dismissed").textContent).toBe(DISMISSED_AT),
    );
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));

    // A failed write must NOT roll back the dismissal nor refetch the stale state.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getByTestId("dismissed").textContent).toBe(DISMISSED_AT);
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it("refetches once after a successful write", async () => {
    const dismissedResponse: OnboardingStateResponse = {
      ...freshResponse(),
      onboarding_state: {
        ...freshOnboardingState(),
        modal_dismissed_at: DISMISSED_AT,
      },
    };
    // First GET is fresh; once the write succeeds the server persists, so the
    // refetch returns the dismissed state.
    mockGet
      .mockResolvedValueOnce({ data: freshResponse() })
      .mockResolvedValue({ data: dismissedResponse });
    mockPost.mockResolvedValue({ data: dismissedResponse });

    renderProvider();
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByText("dismiss"));

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    // onSuccess invalidates -> GET refetches exactly once more.
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("dismissed").textContent).toBe(DISMISSED_AT);
  });
});

describe("OnboardingProvider missing onboarding_state", () => {
  it("renders without throwing when the response omits onboarding_state", async () => {
    // A signed-out / transitional /users/me/onboarding body can lack
    // onboarding_state; the provider must not dereference it (SKY-11430).
    // The key is absent (not explicitly undefined) to mirror the real JSON.
    const responseWithoutState: OnboardingStateResponse = {
      launch_date_at_signup: null,
    };
    mockGet.mockResolvedValue({ data: responseWithoutState });

    renderProvider();

    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    // state resolves to null, so the consumer reads an undefined field rather
    // than crashing on a missing onboarding_state.
    await waitFor(() =>
      expect(screen.getByTestId("dismissed").textContent).toBe("undefined"),
    );
    expect(OnboardingTelemetry.registerVariant).not.toHaveBeenCalled();
  });
});
