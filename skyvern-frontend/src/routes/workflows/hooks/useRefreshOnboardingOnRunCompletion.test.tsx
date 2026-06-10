// @vitest-environment jsdom
import type { ReactNode } from "react";
import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Status } from "@/api/types";
import { useRefreshOnboardingOnRunCompletion } from "./useRefreshOnboardingOnRunCompletion";

const ONBOARDING_KEY = { queryKey: ["userOnboarding"] };
const RUN_ID = "wr_1";

function run(status: Status) {
  return { workflow_run_id: RUN_ID, status };
}

function makeWrapper() {
  const queryClient = new QueryClient();
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { wrapper, invalidateSpy };
}

function onboardingInvalidations(spy: ReturnType<typeof vi.spyOn>): number {
  return spy.mock.calls.filter(
    (call: unknown[]) =>
      JSON.stringify(call[0]) === JSON.stringify(ONBOARDING_KEY),
  ).length;
}

afterEach(() => {
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
});
