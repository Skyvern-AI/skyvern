// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RunHealSummary } from "../types/healTypes";

const { healState } = vi.hoisted(() => ({
  healState: { summary: undefined as RunHealSummary | undefined },
}));

vi.mock("../hooks/useRunHealEpisodesQuery", () => ({
  useRunHealEpisodesQuery: () => ({
    data: healState.summary
      ? { episodes: [], summary: healState.summary }
      : undefined,
  }),
}));

import { RunHealChip } from "./RunHealChip";

describe("RunHealChip", () => {
  beforeEach(() => {
    healState.summary = undefined;
  });

  it("shows the healed-block count when blocks were healed", () => {
    healState.summary = {
      blocks_healed: 2,
      blocks_outcome_risk: [],
      blocks_with_heal_attempt: 2,
    };

    render(<RunHealChip workflowRunId="wr_1" />);

    expect(screen.getByText("2 block(s) self-healed")).toBeDefined();
  });

  it("shows 'Self-heal attempted' with a review badge when every heal failed", () => {
    healState.summary = {
      blocks_healed: 0,
      blocks_outcome_risk: ["wrb_1"],
      blocks_with_heal_attempt: 1,
    };

    render(<RunHealChip workflowRunId="wr_1" />);

    expect(screen.getByText("Self-heal attempted")).toBeDefined();
    expect(screen.getByText("review recommended")).toBeDefined();
    expect(screen.queryByText("0 block(s) self-healed")).toBeNull();
  });

  it("renders nothing when no block attempted a heal", () => {
    healState.summary = {
      blocks_healed: 0,
      blocks_outcome_risk: [],
      blocks_with_heal_attempt: 0,
    };

    const { container } = render(<RunHealChip workflowRunId="wr_1" />);

    expect(container.firstChild).toBeNull();
  });
});
