import { describe, expect, it } from "vitest";

import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../../types/workflowRunTypes";
import { Status } from "@/api/types";
import { buildBlockStatusMap } from "../../studio/runProjections";
import { collectBlocks } from "./useDocRun";

const row = (label: string, block?: Partial<WorkflowRunBlock>) =>
  ({ label, status: "completed", ...block }) as WorkflowRunBlock;
const item = (
  block: WorkflowRunBlock,
  extra: Partial<WorkflowRunTimelineItem> = {},
) =>
  ({
    type: "block",
    block,
    children: [],
    thought: null,
    created_at: "",
    modified_at: "",
    ...extra,
  }) as WorkflowRunTimelineItem;

describe("collectBlocks", () => {
  it("keeps each task_v2 step's own result although the timeline shows its inner rows", () => {
    const inner = row("research", {
      workflow_run_id: "outer",
      block_type: "task_v2",
      duration: 12,
    });
    const outer = row("project", {
      workflow_run_id: "outer",
      block_type: "task_v2",
      duration: 30,
    });
    const blocks = new Map<string, WorkflowRunBlock>();

    collectBlocks(
      [
        item(row("inner_navigation", { workflow_run_id: "child" }), {
          task_v2_blocks: [inner, outer],
        }),
      ],
      blocks,
      "outer",
    );

    expect(blocks.get("research")).toBe(inner);
    expect(blocks.get("project")).toBe(outer);
    expect(blocks.has("inner_navigation")).toBe(false);
  });

  it("only collects blocks owned by the selected workflow run", () => {
    const childBlock = row("navigate", { workflow_run_id: "child" });
    const outerBlock = row("navigate", { workflow_run_id: "outer" });
    const childWrapper = row("child_task", { workflow_run_id: "child" });
    const outerWrapper = row("outer_task", { workflow_run_id: "outer" });
    const blocks = new Map<string, WorkflowRunBlock>();

    collectBlocks(
      [
        item(childBlock, { task_v2_blocks: [childWrapper, outerWrapper] }),
        item(outerBlock),
      ],
      blocks,
      "outer",
    );

    expect(blocks.get("navigate")).toBe(outerBlock);
    expect(blocks.get("outer_task")).toBe(outerWrapper);
    expect(blocks.has("child_task")).toBe(false);
  });
  it("agrees with the canvas on the latest loop iteration", () => {
    const latest = row("repeated", {
      workflow_run_id: "outer",
      status: Status.Failed,
    });
    const timeline = [
      item(latest),
      item(
        row("repeated", { workflow_run_id: "outer", status: Status.Completed }),
      ),
    ];
    const blocks = new Map<string, WorkflowRunBlock>();
    collectBlocks(timeline, blocks, "outer");
    expect(buildBlockStatusMap(timeline).repeated?.status).toBe(
      blocks.get("repeated")?.status,
    );
    expect(blocks.get("repeated")).toBe(latest);
  });
});
