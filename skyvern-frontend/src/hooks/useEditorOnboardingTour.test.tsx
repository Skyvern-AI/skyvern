import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, renderHook, act } from "@testing-library/react";
import type { ReactNode } from "react";
import posthog from "posthog-js";
import { driver, type Config, type DriveStep } from "driver.js";
import { useEditorOnboardingTour } from "./useEditorOnboardingTour";
import {
  OnboardingContext,
  type OnboardingContextValue,
} from "@/store/onboarding/useOnboardingState";
import type { OnboardingState } from "@/store/onboarding/types";
import { useProductTourStore } from "@/store/ProductTourStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

vi.mock("driver.js", () => ({
  driver: vi.fn(() => ({
    drive: vi.fn(),
    destroy: vi.fn(),
    moveNext: vi.fn(),
  })),
}));

vi.mock("posthog-js", () => ({
  default: { capture: vi.fn(), register: vi.fn() },
}));

function captureCount(event: string): number {
  return vi
    .mocked(posthog.capture)
    .mock.calls.filter((call) => call[0] === event).length;
}

vi.mock("@/store/WorkflowPanelStore", async () => {
  const { create } = await import("zustand");
  const useWorkflowPanelStore = create(() => ({
    workflowPanelState: { active: false, content: "parameters" },
  }));
  return { useWorkflowPanelStore };
});

const flagState = vi.hoisted(() => ({
  variant: "template-first" as string | boolean | undefined,
}));
vi.mock("posthog-js/react", () => ({
  useFeatureFlagVariantKey: () => flagState.variant,
}));

type DriverStepCb = (el: Element | undefined, step: DriveStep) => void;
type DriverCloseCb = () => void;

const ANCHOR_ATTRS = [
  "editor-canvas",
  "node-adder",
  "sidebar-region",
  "editor-actions",
];

function addAnchors() {
  for (const attr of ANCHOR_ATTRS) {
    const el = document.createElement("div");
    el.setAttribute("data-tour", attr);
    document.body.appendChild(el);
  }
}

function removeAnchors() {
  document.querySelectorAll("[data-tour]").forEach((el) => el.remove());
}

const DEFAULT_STATE: OnboardingState = {
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

function makeContext(
  overrides: Partial<OnboardingContextValue> = {},
): OnboardingContextValue {
  return {
    state: DEFAULT_STATE,
    isLoading: false,
    updateState: vi.fn(),
    isNewUser: false,
    abVariant: null,
    ...overrides,
  };
}

function wrapWith(ctx: OnboardingContextValue) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <OnboardingContext.Provider value={ctx}>
        {children}
      </OnboardingContext.Provider>
    );
  };
}

function driverMock() {
  return vi.mocked(driver);
}

function lastConfig(): Config {
  const calls = driverMock().mock.calls;
  return calls[calls.length - 1]![0]!;
}

function lastInstance() {
  const results = driverMock().mock.results;
  return results[results.length - 1]!.value as ReturnType<typeof driver>;
}

function nodeAdderStep(config: Config): DriveStep {
  return config.steps!.find(
    (step) => step.element === "[data-tour='node-adder']",
  )!;
}

function highlight(config: Config, step: DriveStep) {
  act(() => {
    (config.onHighlightStarted as DriverStepCb)(undefined, step);
  });
}

function openNodeLibrary(connectingEdgeType = "default") {
  act(() => {
    useWorkflowPanelStore.setState({
      workflowPanelState: {
        active: true,
        content: "nodeLibrary",
        data: { connectingEdgeType },
      },
    });
  });
}

function closePanel() {
  act(() => {
    useWorkflowPanelStore.setState({
      workflowPanelState: { active: false, content: "parameters" },
    });
  });
}

describe("useEditorOnboardingTour", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    flagState.variant = "template-first";
    useProductTourStore.setState({ requestedAt: null });
    useWorkflowPanelStore.setState({
      workflowPanelState: { active: false, content: "parameters" },
    });
    addAnchors();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    removeAnchors();
  });

  describe("auto-trigger logic", () => {
    it("auto-starts for existing users who have not completed the tour", () => {
      const ctx = makeContext({ isNewUser: false });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      expect(driverMock()).not.toHaveBeenCalled();

      act(() => {
        vi.runAllTimers();
      });

      expect(driverMock()).toHaveBeenCalledOnce();
      expect(lastInstance().drive).toHaveBeenCalledOnce();
    });

    it("does not auto-start for new users", () => {
      const ctx = makeContext({ isNewUser: true });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      expect(driverMock()).not.toHaveBeenCalled();
    });

    it("does not auto-start when tour is already completed", () => {
      const ctx = makeContext({
        isNewUser: false,
        state: { ...DEFAULT_STATE, tour_completed_at: "2024-01-01T00:00:00Z" },
      });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      expect(driverMock()).not.toHaveBeenCalled();
    });

    it("does not auto-start when the experiment flag is disabled", () => {
      flagState.variant = false;
      const ctx = makeContext({ isNewUser: false });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      expect(driverMock()).not.toHaveBeenCalled();
    });

    it("does not auto-start while onboarding state is loading", () => {
      const ctx = makeContext({ isLoading: true });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      expect(driverMock()).not.toHaveBeenCalled();
    });
  });

  describe("completion tracking", () => {
    it("persists seen patches on each intermediate step", () => {
      const updateState = vi.fn();
      const ctx = makeContext({ isNewUser: false, updateState });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      const config = lastConfig();
      const steps = config.steps!;

      act(() => {
        (config.onNextClick as DriverStepCb)(undefined, steps[0]!);
      });
      expect(updateState).toHaveBeenCalledWith({ seen_canvas: true });

      highlight(config, steps[1]!);
      openNodeLibrary("default");
      expect(updateState).toHaveBeenCalledWith({ seen_node_adder: true });

      act(() => {
        (config.onNextClick as DriverStepCb)(undefined, steps[2]!);
      });
      expect(updateState).toHaveBeenCalledWith({ seen_sidebar: true });
    });

    it("persists tour_completed_at on the final step", () => {
      const updateState = vi.fn();
      const ctx = makeContext({ isNewUser: false, updateState });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      const config = lastConfig();
      const steps = config.steps!;

      for (let i = 0; i < steps.length - 1; i++) {
        act(() => {
          (config.onNextClick as DriverStepCb)(undefined, steps[i]!);
        });
      }

      updateState.mockClear();

      act(() => {
        (config.onNextClick as DriverStepCb)(
          undefined,
          steps[steps.length - 1]!,
        );
      });

      expect(updateState).toHaveBeenCalledWith(
        expect.objectContaining({
          seen_save_run: true,
          tour_completed_at: expect.any(String),
        }),
      );
      expect(lastInstance().destroy).toHaveBeenCalled();
    });

    it("persists patches when driver passes cloned steps, as driver.js drive() does", () => {
      const updateState = vi.fn();
      const ctx = makeContext({ isNewUser: false, updateState });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      const config = lastConfig();
      const steps = config.steps!;
      const clone = (step: DriveStep): DriveStep => ({
        ...step,
        popover: { ...step.popover },
      });

      act(() => {
        (config.onNextClick as DriverStepCb)(undefined, clone(steps[0]!));
      });
      expect(updateState).toHaveBeenCalledWith({ seen_canvas: true });

      updateState.mockClear();
      act(() => {
        (config.onNextClick as DriverStepCb)(
          undefined,
          clone(steps[steps.length - 1]!),
        );
      });
      expect(updateState).toHaveBeenCalledWith(
        expect.objectContaining({
          tour_completed_at: expect.any(String),
        }),
      );
      expect(lastInstance().destroy).toHaveBeenCalled();
    });
  });

  describe("re-trigger", () => {
    it("starts tour when requestTour is called via store", () => {
      const ctx = makeContext({
        isNewUser: false,
        state: {
          ...DEFAULT_STATE,
          tour_completed_at: "2024-01-01T00:00:00Z",
        },
      });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });
      expect(driverMock()).not.toHaveBeenCalled();

      act(() => {
        useProductTourStore.getState().requestTour();
      });

      expect(driverMock()).toHaveBeenCalledOnce();
      expect(lastInstance().drive).toHaveBeenCalledOnce();
    });

    it("ignores manual re-trigger when the experiment flag is disabled", () => {
      flagState.variant = false;
      const ctx = makeContext({
        isNewUser: false,
        state: {
          ...DEFAULT_STATE,
          tour_completed_at: "2024-01-01T00:00:00Z",
        },
      });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        useProductTourStore.getState().requestTour();
      });

      expect(driverMock()).not.toHaveBeenCalled();
    });

    it("destroys existing tour before restarting", () => {
      const ctx = makeContext({
        isNewUser: false,
        state: {
          ...DEFAULT_STATE,
          tour_completed_at: "2024-01-01T00:00:00Z",
        },
      });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        useProductTourStore.getState().requestTour();
      });

      expect(driverMock()).toHaveBeenCalledTimes(1);
      const firstDestroy = driverMock().mock.results[0]!.value.destroy;

      act(() => {
        useProductTourStore.getState().requestTour();
      });

      expect(firstDestroy).toHaveBeenCalled();
      expect(driverMock()).toHaveBeenCalledTimes(2);
    });
  });

  describe("exit dialog", () => {
    it("shows exit dialog on close click", () => {
      const ctx = makeContext({ isNewUser: false });
      const { result } = renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      expect(result.current.showExitDialog).toBe(false);

      act(() => {
        (lastConfig().onCloseClick as DriverCloseCb)();
      });

      expect(result.current.showExitDialog).toBe(true);
    });

    it("hides exit dialog on cancel", () => {
      const ctx = makeContext({ isNewUser: false });
      const { result } = renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      act(() => {
        (lastConfig().onCloseClick as DriverCloseCb)();
      });
      expect(result.current.showExitDialog).toBe(true);

      act(() => {
        result.current.onExitCancel();
      });
      expect(result.current.showExitDialog).toBe(false);
    });

    it("ends tour and persists completion on confirm", () => {
      const updateState = vi.fn();
      const ctx = makeContext({ isNewUser: false, updateState });
      const { result } = renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      act(() => {
        (lastConfig().onCloseClick as DriverCloseCb)();
      });

      act(() => {
        result.current.onExitConfirm();
      });

      expect(result.current.showExitDialog).toBe(false);
      expect(updateState).toHaveBeenCalledWith(
        expect.objectContaining({ tour_completed_at: expect.any(String) }),
      );
      expect(lastInstance().destroy).toHaveBeenCalled();
    });

    it("pauses the driver layer while the exit dialog is open", () => {
      const ctx = makeContext({ isNewUser: false });
      const { result } = renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });
      expect(document.documentElement.classList.contains("tour-paused")).toBe(
        false,
      );

      act(() => {
        (lastConfig().onCloseClick as DriverCloseCb)();
      });
      expect(document.documentElement.classList.contains("tour-paused")).toBe(
        true,
      );

      act(() => {
        result.current.onExitCancel();
      });
      expect(document.documentElement.classList.contains("tour-paused")).toBe(
        false,
      );
    });

    it("unpauses the driver layer when the tour ends", () => {
      const ctx = makeContext({ isNewUser: false });
      const { result } = renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });
      act(() => {
        (lastConfig().onCloseClick as DriverCloseCb)();
      });
      expect(document.documentElement.classList.contains("tour-paused")).toBe(
        true,
      );

      act(() => {
        result.current.onExitConfirm();
      });
      expect(document.documentElement.classList.contains("tour-paused")).toBe(
        false,
      );
    });
  });

  describe("anchor-missing graceful skip", () => {
    it("does not start when no anchor elements exist", () => {
      removeAnchors();

      const ctx = makeContext({ isNewUser: false });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      expect(driverMock()).not.toHaveBeenCalled();
    });

    it("starts with only available steps when some anchors are missing", () => {
      document.querySelector("[data-tour='node-adder']")?.remove();
      document.querySelector("[data-tour='sidebar-region']")?.remove();

      const ctx = makeContext({ isNewUser: false });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      expect(driverMock()).toHaveBeenCalledOnce();
      const config = lastConfig();
      expect(config.steps).toHaveLength(2);
    });

    it("does not start on re-trigger when no anchors exist", () => {
      const ctx = makeContext({
        isNewUser: false,
        state: {
          ...DEFAULT_STATE,
          tour_completed_at: "2024-01-01T00:00:00Z",
        },
      });
      renderHook(() => useEditorOnboardingTour(), {
        wrapper: wrapWith(ctx),
      });

      act(() => {
        vi.runAllTimers();
      });

      removeAnchors();

      act(() => {
        useProductTourStore.getState().requestTour();
      });

      expect(driverMock()).not.toHaveBeenCalled();
    });
  });

  describe("interactive node-adder gating", () => {
    it("hides the Next button on the node-adder step until the gate is satisfied", () => {
      const ctx = makeContext({ isNewUser: false });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        vi.runAllTimers();
      });

      expect(nodeAdderStep(lastConfig()).popover!.showButtons).not.toContain(
        "next",
      );
    });

    it("shows the Next button on the node-adder step once the gate is satisfied", () => {
      const ctx = makeContext({ isNewUser: false });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        vi.runAllTimers();
      });

      const config = lastConfig();
      highlight(config, nodeAdderStep(config));
      openNodeLibrary("default");
      expect(lastInstance().moveNext).toHaveBeenCalledOnce();

      expect(nodeAdderStep(config).popover!.showButtons).toContain("next");

      vi.mocked(lastInstance().moveNext).mockClear();
      act(() => {
        (config.onNextClick as DriverStepCb)(undefined, nodeAdderStep(config));
      });
      expect(lastInstance().moveNext).toHaveBeenCalledOnce();
    });

    it("advances past step 2 when the + opens the node library", () => {
      const updateState = vi.fn();
      const ctx = makeContext({ isNewUser: false, updateState });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        vi.runAllTimers();
      });

      const config = lastConfig();
      highlight(config, nodeAdderStep(config));
      expect(lastInstance().moveNext).not.toHaveBeenCalled();

      openNodeLibrary("default");

      expect(lastInstance().moveNext).toHaveBeenCalledOnce();
      expect(updateState).toHaveBeenCalledWith({ seen_node_adder: true });
    });

    it("ignores keyboard/Next advance on the node-adder step", () => {
      const updateState = vi.fn();
      const ctx = makeContext({ isNewUser: false, updateState });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        vi.runAllTimers();
      });

      const config = lastConfig();
      highlight(config, nodeAdderStep(config));

      act(() => {
        (config.onNextClick as DriverStepCb)(undefined, nodeAdderStep(config));
      });

      expect(lastInstance().moveNext).not.toHaveBeenCalled();
      expect(updateState).not.toHaveBeenCalledWith({ seen_node_adder: true });
    });

    it("advances on the node-adder step when the library opens via any +", () => {
      const ctx = makeContext({ isNewUser: false });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        vi.runAllTimers();
      });

      const config = lastConfig();
      highlight(config, nodeAdderStep(config));
      openNodeLibrary("edgeWithAddButton");

      expect(lastInstance().moveNext).toHaveBeenCalledOnce();
    });

    it("does not advance when the user is not on the node-adder step", () => {
      const ctx = makeContext({ isNewUser: false });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        vi.runAllTimers();
      });

      const config = lastConfig();
      highlight(config, config.steps![0]!);
      openNodeLibrary("default");

      expect(lastInstance().moveNext).not.toHaveBeenCalled();
    });

    it("completes the tour when the node-adder is the last available step", () => {
      document.querySelector("[data-tour='sidebar-region']")?.remove();
      document.querySelector("[data-tour='editor-actions']")?.remove();

      const updateState = vi.fn();
      const ctx = makeContext({ isNewUser: false, updateState });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        vi.runAllTimers();
      });

      const config = lastConfig();
      expect(config.steps).toHaveLength(2);

      highlight(config, nodeAdderStep(config));
      openNodeLibrary("default");

      expect(lastInstance().moveNext).not.toHaveBeenCalled();
      expect(lastInstance().destroy).toHaveBeenCalled();
      expect(updateState).toHaveBeenCalledWith(
        expect.objectContaining({
          seen_node_adder: true,
          tour_completed_at: expect.any(String),
        }),
      );
    });

    it("re-arms the gate after a re-trigger", () => {
      const ctx = makeContext({
        isNewUser: false,
        state: { ...DEFAULT_STATE, tour_completed_at: "2024-01-01T00:00:00Z" },
      });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        useProductTourStore.getState().requestTour();
      });
      const firstConfig = lastConfig();
      highlight(firstConfig, nodeAdderStep(firstConfig));
      openNodeLibrary("default");
      expect(lastInstance().moveNext).toHaveBeenCalledOnce();

      closePanel();

      act(() => {
        useProductTourStore.getState().requestTour();
      });
      const secondConfig = lastConfig();
      highlight(secondConfig, nodeAdderStep(secondConfig));
      openNodeLibrary("default");
      expect(lastInstance().moveNext).toHaveBeenCalledOnce();
    });
  });

  describe("tour_started emission", () => {
    it("emits tour_started once even when the user returns to the first step", () => {
      const ctx = makeContext({ isNewUser: false });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        vi.runAllTimers();
      });

      const config = lastConfig();
      const steps = config.steps!;
      highlight(config, steps[0]!);
      highlight(config, steps[1]!);
      highlight(config, steps[0]!);

      expect(captureCount("onboarding.tour_started")).toBe(1);
    });

    it("re-emits tour_started on a fresh tour run", () => {
      const ctx = makeContext({
        isNewUser: false,
        state: { ...DEFAULT_STATE, tour_completed_at: "2024-01-01T00:00:00Z" },
      });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        useProductTourStore.getState().requestTour();
      });
      highlight(lastConfig(), lastConfig().steps![0]!);

      act(() => {
        useProductTourStore.getState().requestTour();
      });
      highlight(lastConfig(), lastConfig().steps![0]!);

      expect(captureCount("onboarding.tour_started")).toBe(2);
    });
  });

  describe("experiment arm registration", () => {
    it("registers and persists the arm for existing users before tour telemetry", () => {
      const updateState = vi.fn();
      const ctx = makeContext({ isNewUser: false, updateState });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        vi.runAllTimers();
      });

      expect(vi.mocked(posthog.register)).toHaveBeenCalledWith({
        variant: "template-first",
      });
      expect(updateState).toHaveBeenCalledWith({
        ab_variant: "template-first",
      });
    });

    it("does not reassign the arm when one is already persisted", () => {
      const updateState = vi.fn();
      const ctx = makeContext({
        isNewUser: false,
        updateState,
        state: { ...DEFAULT_STATE, ab_variant: "copilot-first" },
      });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        vi.runAllTimers();
      });

      expect(updateState).not.toHaveBeenCalledWith(
        expect.objectContaining({ ab_variant: expect.anything() }),
      );
    });

    it("does not register an arm when the experiment flag is disabled", () => {
      flagState.variant = false;
      const ctx = makeContext({ isNewUser: false });
      renderHook(() => useEditorOnboardingTour(), { wrapper: wrapWith(ctx) });

      act(() => {
        vi.runAllTimers();
      });

      expect(vi.mocked(posthog.register)).not.toHaveBeenCalled();
    });
  });

  describe("manual start without a provider", () => {
    it("does not start a manual tour when no onboarding provider is mounted", () => {
      renderHook(() => useEditorOnboardingTour());

      act(() => {
        useProductTourStore.getState().requestTour();
      });

      expect(driverMock()).not.toHaveBeenCalled();
    });
  });
});
