import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { driver, type Config } from "driver.js";
import posthog from "posthog-js";

import { useAutoplayStore } from "@/store/useAutoplayStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import {
  OnboardingContext,
  type OnboardingContextValue,
} from "@/store/onboarding/useOnboardingState";
import type { OnboardingState } from "@/store/onboarding/types";
import {
  isOnboardingReplayRequested,
  setOnboardingReplay,
} from "@/util/onboarding/onboardingReplay";

import { useStudioOnboardingTour } from "./useStudioOnboardingTour";

vi.mock("driver.js", () => ({
  driver: vi.fn((config: Config) => ({
    drive: vi.fn(),
    destroy: vi.fn(() => config.onDestroyed?.(undefined, {}, {} as never)),
  })),
}));

vi.mock("posthog-js", () => ({
  default: { capture: vi.fn(), register: vi.fn() },
}));

const STATE: OnboardingState = {
  tour_completed_at: null,
  studio_tour_completed_at: null,
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

type Setup = {
  path?: string;
  isNewUser?: boolean;
  state?: Partial<OnboardingState>;
};

function wait() {
  act(() => {
    vi.advanceTimersByTime(1500);
  });
}

function goLive() {
  act(() => {
    useStudioBrowserStore.setState({
      debugStream: { browserSessionId: "pbs_1", state: "live" },
    });
  });
  wait();
}

function renderTour({
  path = "/agents/wpid_1/studio",
  isNewUser = true,
  state = {},
}: Setup = {}) {
  const ctx: OnboardingContextValue = {
    state: { ...STATE, ...state },
    isLoading: false,
    updateState: vi.fn(),
    updateStateConfirmed: vi.fn(),
    isNewUser,
    abVariant: null,
    recoveryGuidanceAssignment: null,
  };
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter initialEntries={[path]}>
        <OnboardingContext.Provider value={ctx}>
          {children}
        </OnboardingContext.Provider>
      </MemoryRouter>
    );
  }
  const { unmount } = renderHook(
    () => useStudioOnboardingTour(true, "wpid_1"),
    { wrapper: Wrapper },
  );
  wait();
  return { ...ctx, unmount };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(driver).mockClear();
  vi.mocked(posthog.capture).mockClear();
  localStorage.clear();
  useStudioBrowserStore.setState({ debugStream: null, runStream: null });
  useAutoplayStore.getState().clearAutoplay();
  for (const id of ["studio-panel-copilot", "studio-panel-browser"]) {
    const el = document.createElement("section");
    el.id = id;
    document.body.appendChild(el);
  }
  const nav = document.createElement("nav");
  nav.setAttribute("data-tour", "studio-pane-toggles");
  document.body.appendChild(nav);
});

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  vi.useRealTimers();
});

describe("useStudioOnboardingTour", () => {
  it("starts right away for a directly created agent and records it when closed", () => {
    const ctx = renderTour();

    expect(driver).toHaveBeenCalledTimes(1);
    const config = vi.mocked(driver).mock.calls[0]![0] as Config;
    expect(config.steps?.map((s) => s.element)).toEqual([
      "#studio-panel-copilot",
      "#studio-panel-browser",
      "[data-tour='studio-pane-toggles']",
    ]);

    config.onDestroyed?.(undefined, {}, {} as never);
    expect(ctx.updateState).toHaveBeenCalledWith({
      studio_tour_completed_at: expect.any(String),
    });
  });

  it.each<[string, string, string | null]>([
    ["a Copilot handoff", "/agents/wpid_1/studio?via=discover", null],
    ["a generated agent", "/agents/wpid_1/studio", "wpid_1"],
  ])(
    "waits for the run's browser to go live after %s from Home",
    (_label, path, autoplayWpid) => {
      useAutoplayStore.getState().setAutoplay(autoplayWpid, "block_1");
      renderTour({ path });
      expect(driver).not.toHaveBeenCalled();
      goLive();
      expect(driver).toHaveBeenCalledTimes(1);
    },
  );

  it.each<[string, Setup]>([
    [
      "already completed",
      { state: { studio_tour_completed_at: "2026-09-01T00:00:00Z" } },
    ],
    [
      "the user already has a successful run",
      { state: { first_run_at: "2026-09-01T00:00:00Z" } },
    ],
    [
      "opened from a shared run link",
      { path: "/agents/wpid_1/studio?wr=wr_1" },
    ],
    ["opened from a block link", { path: "/agents/wpid_1/studio?bl=block_1" }],
    ["opened on a run step", { path: "/agents/wpid_1/studio?active=step_1" }],
    ["opened on the short run URL", { path: "/runs/wr_1" }],
    ["existing user", { isNewUser: false }],
  ])("does not start when %s", (_label, setup) => {
    renderTour(setup);
    expect(driver).not.toHaveBeenCalled();
  });
  it("a stale run stream from another agent does not start a Home tour", () => {
    useAutoplayStore.getState().setAutoplay("wpid_1", "block_1");
    renderTour();
    act(() => {
      useStudioBrowserStore.setState({
        runStream: { workflowRunId: "wr_old", state: "live" },
      });
    });
    wait();
    expect(driver).not.toHaveBeenCalled();
  });

  it("skips steps whose pane is not on screen", () => {
    document.getElementById("studio-panel-browser")!.remove();
    renderTour();
    const config = vi.mocked(driver).mock.calls[0]![0] as Config;
    expect(config.steps?.map((s) => s.element)).toEqual([
      "#studio-panel-copilot",
      "[data-tour='studio-pane-toggles']",
    ]);
  });

  it("View onboarding replays for a user who already finished it without recording anything", () => {
    setOnboardingReplay(true);
    const ctx = renderTour({
      isNewUser: false,
      state: {
        first_run_at: "2026-09-01T00:00:00Z",
        studio_tour_completed_at: "2026-09-01T00:00:00Z",
      },
    });
    expect(driver).toHaveBeenCalledTimes(1);
    const config = vi.mocked(driver).mock.calls[0]![0] as Config;
    config.onDestroyed?.(undefined, {}, {} as never);

    expect(isOnboardingReplayRequested()).toBe(false);
    expect(ctx.updateState).not.toHaveBeenCalled();
    expect(posthog.capture).not.toHaveBeenCalled();
  });

  it("leaving the studio mid-tour does not record it as seen", () => {
    const ctx = renderTour();
    expect(driver).toHaveBeenCalledTimes(1);

    ctx.unmount();

    expect(ctx.updateState).not.toHaveBeenCalled();
  });
  it("starts only once even if the browser drops and comes back live", () => {
    useAutoplayStore.getState().setAutoplay("wpid_1", "block_1");
    renderTour();
    goLive();
    act(() => {
      useStudioBrowserStore.setState({
        debugStream: { browserSessionId: "pbs_1", state: "connecting" },
      });
    });
    goLive();
    expect(driver).toHaveBeenCalledTimes(1);
  });
});
