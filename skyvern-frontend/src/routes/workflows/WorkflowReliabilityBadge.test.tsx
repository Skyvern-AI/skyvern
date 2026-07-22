// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { WorkflowReliability } from "./types/reliabilityTypes";
import { WorkflowReliabilityBadge } from "./WorkflowReliabilityBadge";

function makeReliability(
  overrides: Partial<WorkflowReliability> = {},
): WorkflowReliability {
  return {
    state: "healthy",
    outcome_risk: false,
    scored: true,
    window_runs: 10,
    healed_runs: 4,
    heal_rate: 0.4,
    consecutive_healed_runs: 1,
    floor_runs: 0,
    outcome_risk_runs: 0,
    ...overrides,
  };
}

describe("WorkflowReliabilityBadge", () => {
  it("renders an amber badge for scored action_needed reliability", () => {
    render(
      <WorkflowReliabilityBadge
        reliability={makeReliability({ state: "action_needed" })}
      />,
    );

    const badge = screen.getByText("Needs a look");
    expect(badge).toBeDefined();
    expect(badge.className).toContain("text-warning");
  });

  it("renders nothing for healthy reliability", () => {
    const { container } = render(
      <WorkflowReliabilityBadge
        reliability={makeReliability({ state: "healthy" })}
      />,
    );

    expect(container.firstChild).toBeNull();
  });

  it("renders nothing for watch reliability", () => {
    const { container } = render(
      <WorkflowReliabilityBadge
        reliability={makeReliability({ state: "watch" })}
      />,
    );

    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when reliability is not scored", () => {
    const { container } = render(
      <WorkflowReliabilityBadge
        reliability={makeReliability({ state: "action_needed", scored: false })}
      />,
    );

    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when reliability is undefined", () => {
    const { container } = render(
      <WorkflowReliabilityBadge reliability={undefined} />,
    );

    expect(container.firstChild).toBeNull();
  });
});
