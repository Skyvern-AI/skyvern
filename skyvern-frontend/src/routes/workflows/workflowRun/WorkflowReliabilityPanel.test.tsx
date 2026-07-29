// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkflowReliability } from "../types/reliabilityTypes";

const { reliabilityState } = vi.hoisted(() => ({
  reliabilityState: {
    reliability: undefined as WorkflowReliability | undefined,
  },
}));

vi.mock("../hooks/useWorkflowReliabilityQuery", () => ({
  useWorkflowReliabilityQuery: () => ({
    data: reliabilityState.reliability,
  }),
}));

import { WorkflowReliabilityPanel } from "./WorkflowReliabilityPanel";

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

describe("WorkflowReliabilityPanel", () => {
  beforeEach(() => {
    reliabilityState.reliability = undefined;
  });

  it("renders nothing for an idle window (no heals, no floor fallbacks)", () => {
    reliabilityState.reliability = makeReliability({
      healed_runs: 0,
      floor_runs: 0,
    });

    const { container } = render(
      <WorkflowReliabilityPanel workflowPermanentId="wpid_1" />,
    );

    expect(container.firstChild).toBeNull();
  });

  it("shows an amber floor-fallback story when action_needed has no heals", () => {
    reliabilityState.reliability = makeReliability({
      state: "action_needed",
      scored: true,
      window_runs: 10,
      healed_runs: 0,
      floor_runs: 3,
      heal_rate: 0,
      consecutive_healed_runs: 0,
    });

    render(<WorkflowReliabilityPanel workflowPermanentId="wpid_1" />);

    const pill = screen.getByText("Needs a look");
    expect(pill).toBeDefined();
    expect(pill.className).toContain("text-warning");
    expect(
      screen.getByText("Fell back to a backup on 3 of the last 10 runs"),
    ).toBeDefined();
  });

  it("shows unscored reliability without any state label", () => {
    reliabilityState.reliability = makeReliability({
      state: "watch",
      scored: false,
      window_runs: 3,
      healed_runs: 3,
      heal_rate: 1,
    });

    render(<WorkflowReliabilityPanel workflowPermanentId="wpid_1" />);

    expect(
      screen.getByText(
        "Self-healed in 3 of the last 3 runs - not enough runs to assess yet.",
      ),
    ).toBeDefined();
    expect(screen.queryByText("Watch")).toBeNull();
    expect(screen.queryByText("Needs a look")).toBeNull();
  });

  it("shows an amber state pill for action_needed reliability", () => {
    reliabilityState.reliability = makeReliability({
      state: "action_needed",
      scored: true,
      window_runs: 10,
      healed_runs: 6,
      heal_rate: 0.6,
      consecutive_healed_runs: 3,
    });

    render(<WorkflowReliabilityPanel workflowPermanentId="wpid_1" />);

    const pill = screen.getByText("Needs a look");
    expect(pill).toBeDefined();
    expect(pill.className).toContain("text-warning");
    expect(
      screen.getByText(
        "Self-healed in 6 of the last 10 runs - 3 in a row - 60% rate",
      ),
    ).toBeDefined();
  });
});
