import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { driver } from "driver.js";
import posthog from "posthog-js";
import { useContextualHints } from "./useContextualHints";
import {
  OnboardingContext,
  type OnboardingContextValue,
} from "@/store/onboarding/useOnboardingState";
import type { OnboardingState } from "@/store/onboarding/types";

type DriverCloseCb = () => void;

vi.mock("driver.js", () => ({
  driver: vi.fn(() => ({ highlight: vi.fn(), destroy: vi.fn() })),
}));

vi.mock("posthog-js", () => ({
  default: { capture: vi.fn(), register: vi.fn() },
}));

const flagState = vi.hoisted(() => ({
  variant: "template-first" as string | boolean | undefined,
}));
vi.mock("posthog-js/react", () => ({
  useFeatureFlagVariantKey: () => flagState.variant,
}));

const COMPLETED_STATE: OnboardingState = {
  tour_completed_at: "2026-06-01T00:00:00Z",
  modal_dismissed_at: null,
  first_save_at: null,
  first_run_at: null,
  ab_variant: "template-first",
  user_intent: null,
  seen_canvas: null,
  seen_node_adder: null,
  seen_sidebar: null,
  seen_save_run: null,
};

function makeCtx(
  overrides: Partial<OnboardingContextValue> = {},
): OnboardingContextValue {
  return {
    state: COMPLETED_STATE,
    isLoading: false,
    updateState: vi.fn(),
    isNewUser: false,
    abVariant: "template-first",
    ...overrides,
  };
}

function Harness() {
  useContextualHints();
  return null;
}

function renderAt(path: string, ctx: OnboardingContextValue) {
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter initialEntries={[path]}>
        <OnboardingContext.Provider value={ctx}>
          {children}
        </OnboardingContext.Provider>
      </MemoryRouter>
    );
  }
  return render(<Harness />, { wrapper: Wrapper });
}

function addAnchor(attr: string, value: string) {
  const el = document.createElement("div");
  el.setAttribute(attr, value);
  document.body.appendChild(el);
}

const DRIVE_AND_SHOW_MS = 500 + 800;

describe("useContextualHints", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    flagState.variant = "template-first";
  });

  afterEach(() => {
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  it("shows the block hint on the editor route once the anchor is present", () => {
    addAnchor("data-tour", "node-adder");
    const ctx = makeCtx();
    renderAt("/workflows/wpid_1/build", ctx);

    vi.advanceTimersByTime(DRIVE_AND_SHOW_MS);

    expect(vi.mocked(driver)).toHaveBeenCalledOnce();
    const instance = vi.mocked(driver).mock.results[0]!.value;
    expect(instance.highlight).toHaveBeenCalledWith(
      expect.objectContaining({ element: "[data-tour='node-adder']" }),
    );
    expect(ctx.updateState).toHaveBeenCalledWith({ seen_hint_block: true });
    expect(posthog.capture).toHaveBeenCalledWith("onboarding.hint_shown", {
      surface: "editor",
      hint_id: "add-another-block",
      layer: 2,
    });
  });

  it("does not show when the Layer-1 tour is not completed", () => {
    addAnchor("data-tour", "node-adder");
    const ctx = makeCtx({
      state: { ...COMPLETED_STATE, tour_completed_at: null },
    });
    renderAt("/workflows/wpid_1/build", ctx);

    vi.advanceTimersByTime(6000);

    expect(vi.mocked(driver)).not.toHaveBeenCalled();
  });

  it("does not show when the experiment flag is not an arm (kill-switch)", () => {
    addAnchor("data-tour", "node-adder");
    flagState.variant = false;
    renderAt("/workflows/wpid_1/build", makeCtx());

    vi.advanceTimersByTime(6000);

    expect(vi.mocked(driver)).not.toHaveBeenCalled();
  });

  it("does not re-show a hint already marked seen", () => {
    addAnchor("data-tour", "node-adder");
    const ctx = makeCtx({
      state: { ...COMPLETED_STATE, seen_hint_block: true },
    });
    renderAt("/workflows/wpid_1/build", ctx);

    vi.advanceTimersByTime(6000);

    expect(vi.mocked(driver)).not.toHaveBeenCalled();
  });

  it("does not show the run hint when its anchor is absent", () => {
    renderAt("/runs", makeCtx());

    vi.advanceTimersByTime(8000);

    expect(vi.mocked(driver)).not.toHaveBeenCalled();
  });

  it("emits hint_dismissed when the user closes the hint", () => {
    addAnchor("data-tour", "node-adder");
    renderAt("/workflows/wpid_1/build", makeCtx());
    vi.advanceTimersByTime(DRIVE_AND_SHOW_MS);

    const config = vi.mocked(driver).mock.calls[0]![0]!;
    (config.onCloseClick as DriverCloseCb)();

    expect(posthog.capture).toHaveBeenCalledWith("onboarding.hint_dismissed", {
      surface: "editor",
      hint_id: "add-another-block",
      layer: 2,
    });
  });
});
