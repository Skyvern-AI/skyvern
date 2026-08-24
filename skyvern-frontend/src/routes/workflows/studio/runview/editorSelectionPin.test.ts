import { describe, expect, test } from "vitest";

import { type WorkflowRunTimelineItem } from "@/routes/workflows/types/workflowRunTypes";

import { resolveEditorSelectionPin } from "./editorSelectionPin";

// Only the slice the resolver reads; a full WorkflowRunBlock adds 30 lines of
// nulls that no assertion here depends on.
function blockItem(
  blockId: string,
  label: string | null,
  actionIds: Array<string> = [],
  children: Array<WorkflowRunTimelineItem> = [],
): WorkflowRunTimelineItem {
  return {
    type: "block",
    thought: null,
    children,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    block: {
      workflow_run_block_id: blockId,
      label,
      actions: actionIds.map((action_id) => ({ action_id })),
    },
  } as unknown as WorkflowRunTimelineItem;
}

const timeline = [
  blockItem("wrb_login", "login", ["act_1", "act_2"]),
  blockItem("wrb_loop", "loop", [], [blockItem("wrb_search", "search")]),
];

const base = {
  editorOpen: true,
  runPaneOpen: true,
  finalized: true,
  blockRun: false,
  timeline,
  selectedBlockLabel: "login",
  systemFocusLabel: null,
  pinnedFrameId: null,
};

describe("resolveEditorSelectionPin", () => {
  test("pins the run block the selected label executed as", () => {
    expect(resolveEditorSelectionPin(base)).toBe("wrb_login");
  });

  test("finds a block nested under a container", () => {
    expect(
      resolveEditorSelectionPin({ ...base, selectedBlockLabel: "search" }),
    ).toBe("wrb_search");
  });

  test("holds still when the pin is already an action inside that block", () => {
    // Without this the timeline→canvas jump would bounce the selection straight
    // back to the block header, off the action the user picked.
    expect(
      resolveEditorSelectionPin({ ...base, pinnedFrameId: "act_2" }),
    ).toBeNull();
  });

  test("holds still when a later occurrence of a looped label is pinned", () => {
    const looped = [
      blockItem("wrb_step_1", "step", ["act_a"]),
      blockItem("wrb_step_2", "step", ["act_b"]),
    ];
    expect(
      resolveEditorSelectionPin({
        ...base,
        timeline: looped,
        selectedBlockLabel: "step",
        pinnedFrameId: "act_b",
      }),
    ).toBeNull();
  });

  test("leaves the pin alone for a block the run never executed", () => {
    expect(
      resolveEditorSelectionPin({ ...base, selectedBlockLabel: "not-run" }),
    ).toBeNull();
  });

  test("never steals the run surfaces off a live run", () => {
    expect(resolveEditorSelectionPin({ ...base, finalized: false })).toBeNull();
  });

  test("never turns an authoring click into a run reference", () => {
    // With the Run pane closed the layout is edit-class; the ?active= this pin
    // mirrors to would flip it to run-class and open the run surfaces.
    expect(
      resolveEditorSelectionPin({ ...base, runPaneOpen: false }),
    ).toBeNull();
  });

  test("never scrubs a block run off its live debug browser", () => {
    expect(resolveEditorSelectionPin({ ...base, blockRun: true })).toBeNull();
  });

  test("never pins a selection the copilot's build-follow wrote", () => {
    // Follow calls focusBlock, which mirrors into ?selected-block=. Pinning
    // that would walk the Run and Browser panes through an old finalized run
    // for the length of the build.
    expect(
      resolveEditorSelectionPin({ ...base, systemFocusLabel: "login" }),
    ).toBeNull();
  });

  test("pins a block the user picked after a follow marked a different one", () => {
    expect(
      resolveEditorSelectionPin({ ...base, systemFocusLabel: "search" }),
    ).toBe("wrb_login");
  });

  test("does nothing while the editor pane is closed", () => {
    expect(
      resolveEditorSelectionPin({ ...base, editorOpen: false }),
    ).toBeNull();
  });
});
