import { describe, expect, test } from "vitest";

import { Status } from "@/api/types";
import { type WorkflowRunTimelineItem } from "@/routes/workflows/types/workflowRunTypes";

import { failingBlockLabel } from "./failingBlock";

function blockItem(
  label: string | null,
  status: Status,
  continueOnFailure = false,
  children: Array<WorkflowRunTimelineItem> = [],
): WorkflowRunTimelineItem {
  return {
    type: "block",
    thought: null,
    children,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    block: {
      workflow_run_block_id: `wrb_${label ?? "anon"}`,
      label,
      status,
      continue_on_failure: continueOnFailure,
      actions: [],
    },
  } as unknown as WorkflowRunTimelineItem;
}

describe("failingBlockLabel", () => {
  test("names the failed block", () => {
    expect(
      failingBlockLabel([
        blockItem("login", Status.Completed),
        blockItem("checkout", Status.Failed),
      ]),
    ).toBe("checkout");
  });

  test("skips continue-on-failure blocks — they cannot end a run", () => {
    expect(
      failingBlockLabel([
        blockItem("checkout", Status.Failed),
        blockItem("optional", Status.Failed, true),
      ]),
    ).toBe("checkout");
  });

  test("finds a failure nested in a container", () => {
    expect(
      failingBlockLabel([
        blockItem("loop", Status.Failed, false, [
          blockItem("search", Status.Failed),
        ]),
      ]),
    ).toBe("search");
  });

  test("skips the finally block, which runs after the real failure", () => {
    expect(
      failingBlockLabel(
        [
          blockItem("checkout", Status.Failed),
          blockItem("cleanup", Status.Failed),
        ],
        "cleanup",
      ),
    ).toBe("checkout");
  });

  test("null for a clean or absent timeline", () => {
    expect(
      failingBlockLabel([blockItem("login", Status.Completed)]),
    ).toBeNull();
    expect(failingBlockLabel(undefined)).toBeNull();
  });
});
