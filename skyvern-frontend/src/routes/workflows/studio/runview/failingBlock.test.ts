import { describe, expect, test } from "vitest";

import { Status } from "@/api/types";
import { type WorkflowRunTimelineItem } from "@/routes/workflows/types/workflowRunTypes";

import { failingBlock } from "./failingBlock";

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

describe("failingBlock", () => {
  test("returns the failed block", () => {
    const timeline = [
      blockItem("login", Status.Completed),
      blockItem("checkout", Status.Failed),
    ];

    expect(failingBlock(timeline)?.workflow_run_block_id).toBe("wrb_checkout");
  });

  test("skips continue-on-failure blocks", () => {
    expect(
      failingBlock([
        blockItem("checkout", Status.Failed),
        blockItem("optional", Status.Failed, true),
      ])?.label,
    ).toBe("checkout");
  });

  test("finds a nested failure", () => {
    expect(
      failingBlock([
        blockItem("loop", Status.Failed, false, [
          blockItem("search", Status.Failed),
        ]),
      ])?.label,
    ).toBe("search");
  });

  test("prefers a pre-finally failure but keeps a finally-only target", () => {
    expect(
      failingBlock(
        [
          blockItem("checkout", Status.Failed),
          blockItem("cleanup", Status.Failed),
        ],
        "cleanup",
      )?.label,
    ).toBe("checkout");
    expect(
      failingBlock([blockItem("cleanup", Status.Failed)], "cleanup")?.label,
    ).toBe("cleanup");
  });

  test("keeps an unlabeled failed block as the recovery target", () => {
    expect(
      failingBlock([blockItem(null, Status.Failed)])?.workflow_run_block_id,
    ).toBe("wrb_anon");
  });

  test("returns null for a clean or absent timeline", () => {
    expect(failingBlock([blockItem("login", Status.Completed)])).toBeNull();
    expect(failingBlock(undefined)).toBeNull();
  });
});
