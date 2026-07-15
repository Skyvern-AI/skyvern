// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { HealEpisodeView } from "../types/healTypes";

function makeEpisode(overrides: Partial<HealEpisodeView>): HealEpisodeView {
  return {
    heal_episode_id: "he_1",
    workflow_permanent_id: "wpid_1",
    workflow_id: "wf_1",
    workflow_run_id: "wr_1",
    workflow_run_block_id: "wrb_1",
    block_label: "Login",
    engine: "harness",
    status: "fired_completed",
    skip_reason: null,
    snapshot_available: true,
    convergence_eligible: true,
    parameter_binding_keys: [],
    exception_class: null,
    failing_line: null,
    matched_step_index: 3,
    escalation_task_id: null,
    wall_clock_ms: 820,
    action_count: 2,
    output_obligation: "observed",
    dom_snapshot_artifact_id: "art_dom",
    scout_transcript_artifact_id: "art_scout",
    screenshot_artifact_id: "art_ss",
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

const { healState } = vi.hoisted(() => ({
  healState: { episodes: [] as Array<HealEpisodeView> },
}));

vi.mock("../hooks/useRunHealEpisodesQuery", () => ({
  useRunHealEpisodesQuery: () => ({
    data: {
      episodes: healState.episodes,
      summary: {
        blocks_healed: 1,
        blocks_outcome_risk: [],
        blocks_with_heal_attempt: 1,
      },
    },
  }),
}));

import { BlockHealPanel } from "./BlockHealPanel";

describe("BlockHealPanel", () => {
  beforeEach(() => {
    healState.episodes = [makeEpisode({})];
  });

  it("renders episode status, timing, actions, and evidence chips", () => {
    render(<BlockHealPanel workflowRunId="wr_1" workflowRunBlockId="wrb_1" />);

    expect(screen.getByText("Runtime healing")).toBeDefined();
    expect(screen.getByText("Self-healed")).toBeDefined();
    expect(screen.getByText("primary")).toBeDefined();
    expect(screen.getByText("820 ms")).toBeDefined();
    expect(screen.getByText("2 actions")).toBeDefined();
    expect(screen.getByText("DOM snapshot")).toBeDefined();
    expect(screen.getByText("scout transcript")).toBeDefined();
    expect(screen.getByText("screenshot")).toBeDefined();
    expect(
      screen.getByText(
        "Recovered this run — your workflow version is unchanged.",
      ),
    ).toBeDefined();
  });

  it("does not claim recovery when the block only failed or was skipped", () => {
    healState.episodes = [
      makeEpisode({
        heal_episode_id: "he_failed",
        status: "fired_failed",
        wall_clock_ms: 2000,
        action_count: 5,
        dom_snapshot_artifact_id: null,
        scout_transcript_artifact_id: null,
        screenshot_artifact_id: null,
      }),
    ];

    render(<BlockHealPanel workflowRunId="wr_1" workflowRunBlockId="wrb_1" />);

    expect(screen.getByText("Heal failed")).toBeDefined();
    expect(
      screen.getByText("Your workflow version is unchanged."),
    ).toBeDefined();
    expect(
      screen.queryByText(
        "Recovered this run — your workflow version is unchanged.",
      ),
    ).toBeNull();
  });
});
