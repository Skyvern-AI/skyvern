import { describe, expect, it } from "vitest";

import type { WorkflowBlock } from "../../types/workflowTypes";
import type { DebugSessionRun } from "../../hooks/useDebugSessionRunsQuery";
import {
  buildBlockTypeByLabel,
  getRunActivityKey,
  getRunDurationLabel,
  getRunModeLabel,
  getRunStatusKind,
  getRunStatusLabel,
} from "./runActivity";

function makeRun(partial: Partial<DebugSessionRun>): DebugSessionRun {
  return {
    ai_fallback: null,
    block_label: "block_1",
    browser_session_id: "bs_1",
    code_gen: null,
    debug_session_id: "ds_1",
    failure_reason: null,
    output_parameter_id: "op_1",
    run_with: null,
    script_run_id: null,
    status: "completed",
    workflow_id: "w_1",
    workflow_permanent_id: "wpid_1",
    workflow_run_id: "wr_1",
    created_at: "2026-06-15T10:00:00Z",
    queued_at: null,
    started_at: null,
    finished_at: null,
    ...partial,
  };
}

describe("getRunStatusKind", () => {
  it("maps terminal statuses", () => {
    expect(getRunStatusKind("completed", false)).toBe("success");
    expect(getRunStatusKind("failed", false)).toBe("failure");
    expect(getRunStatusKind("terminated", false)).toBe("failure");
    expect(getRunStatusKind("timed_out", false)).toBe("failure");
    expect(getRunStatusKind("canceled", false)).toBe("failure");
    expect(getRunStatusKind("queued", false)).toBe("pending");
    expect(getRunStatusKind("created", false)).toBe("pending");
    expect(getRunStatusKind("skipped", false)).toBe("neutral");
  });

  it("only spins a running status while the workflow is live", () => {
    expect(getRunStatusKind("running", true)).toBe("running");
    expect(getRunStatusKind("running", false)).toBe("neutral");
  });
});

describe("getRunDurationLabel", () => {
  it("formats started→finished elapsed time", () => {
    const run = makeRun({
      started_at: "2026-06-15T10:00:00Z",
      finished_at: "2026-06-15T10:00:05Z",
    });
    expect(getRunDurationLabel(run)).toBe("5s");
  });

  it("falls back to created_at when started_at is absent", () => {
    const run = makeRun({
      created_at: "2026-06-15T10:00:00Z",
      started_at: null,
      finished_at: "2026-06-15T10:01:30Z",
    });
    expect(getRunDurationLabel(run)).toBe("1m 30s");
  });

  it("returns null without a finish time or with a negative span", () => {
    expect(getRunDurationLabel(makeRun({ finished_at: null }))).toBeNull();
    expect(
      getRunDurationLabel(
        makeRun({
          started_at: "2026-06-15T10:00:05Z",
          finished_at: "2026-06-15T10:00:00Z",
        }),
      ),
    ).toBeNull();
  });
});

describe("getRunModeLabel", () => {
  it("distinguishes explicit run modes from missing or unknown values", () => {
    expect(getRunModeLabel(makeRun({ run_with: "code" }))).toBe("Code");
    expect(getRunModeLabel(makeRun({ run_with: "agent" }))).toBe("Agent");
    expect(getRunModeLabel(makeRun({ run_with: null }))).toBe("Unknown");
    expect(getRunModeLabel(makeRun({ run_with: "legacy" }))).toBe("Unknown");
  });
});

describe("getRunStatusLabel", () => {
  it("humanizes snake_case statuses", () => {
    expect(getRunStatusLabel("timed_out")).toBe("Timed Out");
    expect(getRunStatusLabel("completed")).toBe("Completed");
  });
});

describe("getRunActivityKey", () => {
  it("disambiguates block runs that share a workflow run", () => {
    expect(
      getRunActivityKey(
        makeRun({ workflow_run_id: "wr_1", block_label: "block_2" }),
      ),
    ).toBe("wr_1:block_2");
  });
});

describe("buildBlockTypeByLabel", () => {
  it("maps top-level and nested loop block labels", () => {
    const blocks = [
      { label: "nav", block_type: "navigation" },
      {
        label: "loop",
        block_type: "for_loop",
        loop_blocks: [{ label: "inner", block_type: "task" }],
      },
    ] as unknown as Array<WorkflowBlock>;
    const map = buildBlockTypeByLabel(blocks);
    expect(map.get("nav")).toBe("navigation");
    expect(map.get("loop")).toBe("for_loop");
    expect(map.get("inner")).toBe("task");
  });
});
