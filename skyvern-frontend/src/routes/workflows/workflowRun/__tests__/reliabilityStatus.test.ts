import { describe, expect, it } from "vitest";

import {
  reliabilityHasActivity,
  reliabilityLabel,
  reliabilityShowsState,
  reliabilityTone,
} from "../reliabilityStatus";
import type { WorkflowReliability } from "../../types/reliabilityTypes";

function makeReliability(
  overrides: Partial<WorkflowReliability> = {},
): WorkflowReliability {
  return {
    state: "healthy",
    outcome_risk: false,
    scored: true,
    window_runs: 12,
    healed_runs: 2,
    heal_rate: 2 / 12,
    consecutive_healed_runs: 1,
    floor_runs: 0,
    outcome_risk_runs: 0,
    ...overrides,
  };
}

describe("reliability status helpers", () => {
  it("maps reliability states to user-facing labels", () => {
    expect(reliabilityLabel("healthy")).toBe("Healthy");
    expect(reliabilityLabel("watch")).toBe("Watch");
    expect(reliabilityLabel("action_needed")).toBe("Needs a look");
  });

  it("keeps reliability tones neutral or amber only", () => {
    expect(reliabilityTone("healthy")).toBe("neutral");
    expect(reliabilityTone("watch")).toBe("amber");
    expect(reliabilityTone("action_needed")).toBe("amber");
    expect(reliabilityTone("healthy")).not.toBe("destructive");
    expect(reliabilityTone("watch")).not.toBe("destructive");
    expect(reliabilityTone("action_needed")).not.toBe("destructive");
  });

  it("reports activity for heals or floor fallbacks, but not an idle window", () => {
    expect(
      reliabilityHasActivity(
        makeReliability({ healed_runs: 0, floor_runs: 0 }),
      ),
    ).toBe(false);
    expect(reliabilityHasActivity(makeReliability({ healed_runs: 1 }))).toBe(
      true,
    );
    expect(
      reliabilityHasActivity(
        makeReliability({ healed_runs: 0, floor_runs: 2 }),
      ),
    ).toBe(true);
  });

  it("shows state only when reliability is scored", () => {
    expect(reliabilityShowsState(makeReliability({ scored: true }))).toBe(true);
    expect(reliabilityShowsState(makeReliability({ scored: false }))).toBe(
      false,
    );
  });
});
