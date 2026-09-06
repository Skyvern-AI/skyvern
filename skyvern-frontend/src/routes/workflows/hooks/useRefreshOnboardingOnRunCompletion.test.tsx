// @vitest-environment jsdom
import type { ReactNode } from "react";
import { act, renderHook } from "@testing-library/react";
import {
  QueryClient,
  QueryClientProvider,
  QueryObserver,
} from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Status } from "@/api/types";
import type {
  OnboardingStateResponse,
  RecoveryGuidanceAssignment,
} from "@/store/onboarding/types";
import { useRefreshOnboardingOnRunCompletion } from "./useRefreshOnboardingOnRunCompletion";

const ONBOARDING_KEY = { queryKey: ["userOnboarding", "user-a"] };
const ONBOARDING_PREFIX = { queryKey: ["userOnboarding"] };
const RUN_ID = "wr_1";

function run(status: Status, workflowRunId = RUN_ID) {
  return { workflow_run_id: workflowRunId, status };
}

function makeWrapper() {
  const queryClient = new QueryClient();
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { wrapper, invalidateSpy };
}

const assignment: RecoveryGuidanceAssignment = {
  experiment_version: "sky-13471-recovery-guidance-v1",
  organization_id: "org_1",
  eligible_run_id: RUN_ID,
  eligible_at: "2026-08-14T12:00:00Z",
  arm: "treatment",
};

function onboardingResponse(
  recoveryGuidanceAssignment: RecoveryGuidanceAssignment | null,
): OnboardingStateResponse {
  return {
    onboarding_state: {
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
    },
    launch_date_at_signup: "2026-08-01T00:00:00Z",
    recovery_guidance_assignment: recoveryGuidanceAssignment,
  };
}

function makeRaceWrapper(firstAssignment: RecoveryGuidanceAssignment | null) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { gcTime: Infinity, retry: false } },
  });
  queryClient.setQueryData(ONBOARDING_KEY.queryKey, onboardingResponse(null));
  const fetchOnboarding = vi
    .fn()
    .mockResolvedValueOnce(onboardingResponse(firstAssignment))
    .mockResolvedValueOnce(onboardingResponse(assignment));

  const observer = new QueryObserver<OnboardingStateResponse>(queryClient, {
    queryKey: ONBOARDING_KEY.queryKey,
    queryFn: fetchOnboarding,
    staleTime: Infinity,
  });
  const unsubscribe = observer.subscribe(() => undefined);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { fetchOnboarding, queryClient, unsubscribe, wrapper };
}

function onboardingInvalidations(spy: ReturnType<typeof vi.spyOn>): number {
  return spy.mock.calls.filter(
    (call: unknown[]) =>
      JSON.stringify(call[0]) === JSON.stringify(ONBOARDING_PREFIX),
  ).length;
}

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("useRefreshOnboardingOnRunCompletion", () => {
  it("invalidates userOnboarding when an observed running run finalizes", () => {
    const { wrapper, invalidateSpy } = makeWrapper();
    const { rerender } = renderHook(
      ({ wr }) => useRefreshOnboardingOnRunCompletion(wr),
      { wrapper, initialProps: { wr: run(Status.Running) } },
    );
    expect(onboardingInvalidations(invalidateSpy)).toBe(0);

    rerender({ wr: run(Status.Completed) });
    expect(onboardingInvalidations(invalidateSpy)).toBe(1);
  });

  it("invalidates once when a run is already finalized on first observation", () => {
    const { wrapper, invalidateSpy } = makeWrapper();
    renderHook(({ wr }) => useRefreshOnboardingOnRunCompletion(wr), {
      wrapper,
      initialProps: { wr: run(Status.Completed) },
    });
    expect(onboardingInvalidations(invalidateSpy)).toBe(1);
  });

  it("does not invalidate while the run is not finalized", () => {
    const { wrapper, invalidateSpy } = makeWrapper();
    renderHook(({ wr }) => useRefreshOnboardingOnRunCompletion(wr), {
      wrapper,
      initialProps: { wr: run(Status.Running) },
    });
    expect(onboardingInvalidations(invalidateSpy)).toBe(0);
  });

  it("invalidates only once after completion across re-renders", () => {
    const { wrapper, invalidateSpy } = makeWrapper();
    const { rerender } = renderHook(
      ({ wr }) => useRefreshOnboardingOnRunCompletion(wr),
      { wrapper, initialProps: { wr: run(Status.Running) } },
    );
    rerender({ wr: run(Status.Completed) });
    rerender({ wr: run(Status.Completed) });
    expect(onboardingInvalidations(invalidateSpy)).toBe(1);
  });

  it("re-invalidates once when a failed-run assignment misses the first fetch", async () => {
    vi.useFakeTimers();
    const { fetchOnboarding, queryClient, wrapper } = makeRaceWrapper(null);

    renderHook(({ wr }) => useRefreshOnboardingOnRunCompletion(wr), {
      wrapper,
      initialProps: { wr: run(Status.Failed) },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(fetchOnboarding).toHaveBeenCalledTimes(1);
    expect(
      queryClient.getQueryData<OnboardingStateResponse>(ONBOARDING_KEY.queryKey)
        ?.recovery_guidance_assignment,
    ).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });

    expect(fetchOnboarding).toHaveBeenCalledTimes(2);
    expect(
      queryClient.getQueryData<OnboardingStateResponse>(ONBOARDING_KEY.queryKey)
        ?.recovery_guidance_assignment,
    ).toEqual(assignment);
  });

  it("retries a missing failed-run assignment only once per mount", async () => {
    vi.useFakeTimers();
    const { wrapper, invalidateSpy } = makeWrapper();
    const { rerender } = renderHook(
      ({ wr }) => useRefreshOnboardingOnRunCompletion(wr),
      { wrapper, initialProps: { wr: run(Status.Failed) } },
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });
    expect(onboardingInvalidations(invalidateSpy)).toBe(2);

    rerender({ wr: run(Status.Failed, "wr_2") });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });

    expect(onboardingInvalidations(invalidateSpy)).toBe(3);
  });

  it("does not re-invalidate when the first fetch has the failed-run assignment", async () => {
    vi.useFakeTimers();
    const { fetchOnboarding, wrapper } = makeRaceWrapper(assignment);

    renderHook(({ wr }) => useRefreshOnboardingOnRunCompletion(wr), {
      wrapper,
      initialProps: { wr: run(Status.Failed) },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchOnboarding).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });

    expect(fetchOnboarding).toHaveBeenCalledTimes(1);
  });

  it("finds the matching assignment when another cached user entry comes first", async () => {
    vi.useFakeTimers();
    const queryClient = new QueryClient();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    queryClient.setQueryData(
      ["userOnboarding", "previous-user"],
      onboardingResponse(null),
    );
    queryClient.setQueryData(
      ONBOARDING_KEY.queryKey,
      onboardingResponse(assignment),
    );
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    renderHook(({ wr }) => useRefreshOnboardingOnRunCompletion(wr), {
      wrapper,
      initialProps: { wr: run(Status.Failed) },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000);
    });

    expect(onboardingInvalidations(invalidateSpy)).toBe(1);
  });
});
