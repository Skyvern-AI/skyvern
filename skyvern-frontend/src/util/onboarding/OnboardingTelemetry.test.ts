import { describe, it, expect, vi, beforeEach } from "vitest";
import posthog from "posthog-js";
import { OnboardingTelemetry } from "./OnboardingTelemetry";

vi.mock("posthog-js", () => ({
  default: { capture: vi.fn(), register: vi.fn() },
}));

describe("OnboardingTelemetry hint events", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("hintShown captures a layer-2 event with the hint id", () => {
    OnboardingTelemetry.hintShown("editor", "add-another-block");
    expect(posthog.capture).toHaveBeenCalledWith("onboarding.hint_shown", {
      surface: "editor",
      hint_id: "add-another-block",
      layer: 2,
    });
  });

  it("hintDismissed captures a layer-2 event with the hint id", () => {
    OnboardingTelemetry.hintDismissed("runs", "run-recording");
    expect(posthog.capture).toHaveBeenCalledWith("onboarding.hint_dismissed", {
      surface: "runs",
      hint_id: "run-recording",
      layer: 2,
    });
  });
});
