// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type { WorkflowReliability } from "../types/reliabilityTypes";

const { mockState } = vi.hoisted(() => ({
  mockState: {
    workflowRun: {
      workflow: { workflow_permanent_id: "wpid_1" },
    },
    reliability: undefined as WorkflowReliability | undefined,
  },
}));

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: mockState.workflowRun,
  }),
}));

vi.mock("../hooks/useWorkflowReliabilityQuery", () => ({
  useWorkflowReliabilityQuery: () => ({
    data: mockState.reliability,
  }),
}));

import { RunReliabilityUplink } from "./RunReliabilityUplink";

function makeReliability(
  overrides: Partial<WorkflowReliability> = {},
): WorkflowReliability {
  return {
    state: "healthy",
    outcome_risk: false,
    scored: true,
    window_runs: 10,
    healed_runs: 2,
    heal_rate: 0.2,
    consecutive_healed_runs: 1,
    floor_runs: 0,
    outcome_risk_runs: 0,
    ...overrides,
  };
}

describe("RunReliabilityUplink", () => {
  beforeEach(() => {
    mockState.workflowRun = {
      workflow: { workflow_permanent_id: "wpid_1" },
    };
    mockState.reliability = undefined;
  });

  it("renders nothing when reliability is not scored", () => {
    mockState.reliability = makeReliability({
      state: "watch",
      scored: false,
      healed_runs: 3,
    });

    const { container } = render(
      <MemoryRouter>
        <RunReliabilityUplink workflowRunId="wr_1" />
      </MemoryRouter>,
    );

    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when there is no heal or floor activity", () => {
    mockState.reliability = makeReliability({
      state: "action_needed",
      healed_runs: 0,
      floor_runs: 0,
    });

    const { container } = render(
      <MemoryRouter>
        <RunReliabilityUplink workflowRunId="wr_1" />
      </MemoryRouter>,
    );

    expect(container.firstChild).toBeNull();
  });

  it("links with a floor-fallback story when action_needed has no heals", () => {
    mockState.reliability = makeReliability({
      state: "action_needed",
      scored: true,
      healed_runs: 0,
      floor_runs: 3,
      window_runs: 10,
    });

    render(
      <MemoryRouter>
        <RunReliabilityUplink workflowRunId="wr_1" />
      </MemoryRouter>,
    );

    const link = screen.getByRole("link", {
      name: "This workflow fell back to a backup on 3 of the last 10 runs →",
    });
    expect(link).toBeDefined();
    expect(link.getAttribute("href")).toBe("/agents/wpid_1/runs");
  });

  it("links to workflow reliability when scored and not healthy", () => {
    mockState.reliability = makeReliability({
      state: "watch",
      scored: true,
      healed_runs: 4,
      window_runs: 10,
    });

    render(
      <MemoryRouter>
        <RunReliabilityUplink workflowRunId="wr_1" />
      </MemoryRouter>,
    );

    const link = screen.getByRole("link", {
      name: "This workflow self-healed 4 of the last 10 runs →",
    });
    expect(link).toBeDefined();
    expect(link.getAttribute("href")).toBe("/agents/wpid_1/runs");
  });
});
