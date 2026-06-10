import { describe, it, expect, vi, beforeEach } from "vitest";
import posthog from "posthog-js";
import { RecoveryGuidanceTelemetry } from "./recoveryGuidanceTelemetry";

vi.mock("posthog-js", () => ({
  default: { capture: vi.fn() },
}));

describe("RecoveryGuidanceTelemetry", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("recoveryGuidanceShown captures the surface, category, and path count", () => {
    RecoveryGuidanceTelemetry.recoveryGuidanceShown(
      "runs",
      "invalid_credentials",
      2,
    );
    expect(posthog.capture).toHaveBeenCalledWith(
      "onboarding.recovery_guidance_shown",
      {
        surface: "runs",
        failure_category: "invalid_credentials",
        path_count: 2,
      },
    );
  });

  it("recoveryPathChosen captures the chosen path id", () => {
    RecoveryGuidanceTelemetry.recoveryPathChosen(
      "runs",
      "network_error",
      "retry",
    );
    expect(posthog.capture).toHaveBeenCalledWith(
      "onboarding.recovery_path_chosen",
      { surface: "runs", failure_category: "network_error", path_id: "retry" },
    );
  });

  it("recoveryOutcome captures the chosen path id and downstream outcome", () => {
    RecoveryGuidanceTelemetry.recoveryOutcome(
      "runs",
      null,
      "view_docs",
      "opened",
    );
    expect(posthog.capture).toHaveBeenCalledWith(
      "onboarding.recovery_outcome",
      {
        surface: "runs",
        failure_category: null,
        path_id: "view_docs",
        outcome: "opened",
      },
    );
  });

  it("swallows posthog errors so telemetry never breaks the UI", () => {
    vi.mocked(posthog.capture).mockImplementationOnce(() => {
      throw new Error("posthog unavailable");
    });
    expect(() =>
      RecoveryGuidanceTelemetry.recoveryGuidanceShown("runs", null, 3),
    ).not.toThrow();
  });
});
