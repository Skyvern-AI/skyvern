import { describe, expect, it } from "vitest";

import {
  AUTHORING_TOOLS,
  RUN_TOOLS,
  showPhaseChecklist,
} from "./copilotPhases";
import {
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  toolActivityDisplayLabel,
} from "./narrativeState";

const turn = (
  overrides: Partial<TurnNarrativeState> = {},
): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  designStarted: true,
  ...overrides,
});

describe("AUTHORING_TOOLS / RUN_TOOLS", () => {
  it("update_workflow is authoring-only, not a run tool (Codex catch)", () => {
    expect(AUTHORING_TOOLS.has("update_workflow")).toBe(true);
    expect(RUN_TOOLS.has("update_workflow")).toBe(false);
  });

  it("update_and_run_blocks is both authoring and a run tool", () => {
    expect(AUTHORING_TOOLS.has("update_and_run_blocks")).toBe(true);
    expect(RUN_TOOLS.has("update_and_run_blocks")).toBe(true);
  });

  it("edit_block_and_run is both authoring and a run tool", () => {
    expect(AUTHORING_TOOLS.has("edit_block_and_run")).toBe(true);
    expect(RUN_TOOLS.has("edit_block_and_run")).toBe(true);
  });

  it("run_blocks_and_collect_debug is a run tool only, not authoring", () => {
    expect(RUN_TOOLS.has("run_blocks_and_collect_debug")).toBe(true);
    expect(AUTHORING_TOOLS.has("run_blocks_and_collect_debug")).toBe(false);
  });
});

describe("toolActivityDisplayLabel — discovery tools (SKY-12385)", () => {
  it("labels discover_workflow_entrypoint and inspect_page_for_composition", () => {
    expect(toolActivityDisplayLabel("discover_workflow_entrypoint")).toBe(
      "Finding the entry page",
    );
    expect(toolActivityDisplayLabel("inspect_page_for_composition")).toBe(
      "Inspecting the page",
    );
    expect(toolActivityDisplayLabel("skyvern_frame_list")).toBe(
      "Finding embedded pages",
    );
    expect(toolActivityDisplayLabel("skyvern_frame_switch")).toBe(
      "Opening embedded page",
    );
    expect(toolActivityDisplayLabel("skyvern_frame_main")).toBe(
      "Returning to main page",
    );
  });

  it("labels fill_credential_field without naming any credential", () => {
    expect(toolActivityDisplayLabel("fill_credential_field")).toBe(
      "Entering saved credentials",
    );
  });

  it("still falls back to Working for unmapped tools", () => {
    expect(toolActivityDisplayLabel("some_unmapped_tool")).toBe("Working");
  });
});

describe("showPhaseChecklist", () => {
  it("false for a clarify terminal turn with no draft and no blocks", () => {
    expect(
      showPhaseChecklist(
        turn({ terminal: "response", draft: null, blocks: [] }),
      ),
    ).toBe(false);
  });

  it("true for a hydrated build payload", () => {
    expect(
      showPhaseChecklist(
        turn({
          terminal: "response",
          draft: { blockCount: 1, blockLabels: ["a"], summary: null },
        }),
      ),
    ).toBe(true);
  });

  it("true for any live (non-terminal) turn once design has started", () => {
    expect(showPhaseChecklist(turn({ terminal: null }))).toBe(true);
  });
});
