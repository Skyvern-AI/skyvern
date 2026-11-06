import { describe, expect, it } from "vitest";

import { Status } from "@/api/types";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../../types/workflowRunTypes";
import {
  collectLoopProgress,
  docLoopProgressText,
  docRunEndMessage,
  docRunRowsSettled,
  docTagState,
  stepsUnknownToRun,
  type DocLoopProgress,
} from "./docRunState";
import { collectBlocks } from "./useDocRun";

const RUN = "wr_1";
const row = (label: string, block: Partial<WorkflowRunBlock> = {}) =>
  ({
    label,
    status: Status.Completed,
    workflow_run_id: RUN,
    block_type: "navigation",
    current_index: null,
    current_value: null,
    loop_values: null,
    failure_reason: null,
    ...block,
  }) as WorkflowRunBlock;
const item = (
  block: WorkflowRunBlock,
  children: WorkflowRunTimelineItem[] = [],
) =>
  ({
    type: "block",
    block,
    children,
    thought: null,
    created_at: "",
    modified_at: "",
  }) as WorkflowRunTimelineItem;

function project(timeline: WorkflowRunTimelineItem[]) {
  const blocks = new Map<string, WorkflowRunBlock>();
  const loops = new Map<string, DocLoopProgress>();
  collectBlocks(timeline, blocks, RUN);
  collectLoopProgress(timeline, RUN, loops);
  return { blocks, loops };
}

describe("Doc run state", () => {
  it("gives each step's tag its live state, and Not reached once the run settles", () => {
    const { blocks } = project([
      item(row("open_site")),
      item(row("fill_form", { status: Status.Running })),
      item(row("approve", { status: Status.Paused })),
      item(row("download", { status: Status.Failed })),
    ]);
    const state = (label: string, live: boolean) =>
      docTagState(blocks.get(label), live, true);

    expect(state("open_site", true)).toBe("succeeded");
    expect(state("fill_form", true)).toBe("running");
    expect(state("approve", true)).toBe("waiting");
    expect(state("download", true)).toBe("failed");
    expect(state("send_email", true)).toBeNull();
    expect(state("send_email", false)).toBe("notReached");
    // A step left "running" by a stopped run never finished.
    expect(state("fill_form", false)).toBe("notReached");
    expect(docTagState(undefined, false, false)).toBeNull();
  });

  it("counts loop items and reports failed items skipped at the end", () => {
    const loop = row("each_invoice", {
      block_type: "for_loop",
      loop_values: ["a", "b", "c"],
    });
    const timeline = [
      item(loop, [
        item(row("get_pdf", { current_index: 2, current_value: "c" })),
        item(
          row("get_pdf", {
            current_index: 1,
            current_value: "b",
            status: Status.Failed,
            failure_reason: "download link expired",
          }),
        ),
        item(row("get_pdf", { current_index: 0, current_value: "a" })),
      ]),
    ];
    const { blocks, loops } = project(timeline);
    const progress = loops.get("each_invoice")!;

    expect(progress.items.map((i) => [i.value, i.state])).toEqual([
      ["a", "succeeded"],
      ["b", "failed"],
      ["c", "succeeded"],
    ]);
    expect(docLoopProgressText(progress, true)).toBe("Item 3 of 3 · 1 failed");
    expect(docLoopProgressText(progress, false)).toBe("3 items · 1 failed");
    expect(docRunEndMessage({ status: Status.Completed }, blocks, loops)).toBe(
      "Run finished · 1 item failed and was skipped",
    );
  });

  it("states a failed loop item with the same headline as the Run panel", () => {
    const loop = row("each_row", {
      block_type: "for_loop",
      loop_values: ["a"],
    });
    const { loops } = project([
      item(loop, [
        item(
          row("fill_dates", {
            block_type: "code",
            current_index: 0,
            status: Status.Failed,
            error_codes: ["user_code_error"],
            failure_reason:
              "CodeBlock failed with ValueError at line 4: Could not find the input.",
            output: {
              code_failure: {
                exception_class: "ValueError",
                message: "Could not find the input.",
                line: 4,
              },
            },
          }),
        ),
      ]),
    ]);

    expect(loops.get("each_row")!.items[0]!.failureHeadline).toBe(
      "Could not find the input.",
    );
  });

  it("names how the run ended", () => {
    const plain = project([item(row("open_site"))]);
    const stopped = project([
      item(row("open_site")),
      item(
        row("stop_here", {
          block_type: "terminate",
          status: Status.Terminated,
        }),
      ),
    ]);
    const failed = project([item(row("open_site", { status: Status.Failed }))]);

    expect(
      docRunEndMessage({ status: Status.Completed }, plain.blocks, plain.loops),
    ).toBe("Run finished");
    expect(
      docRunEndMessage(
        { status: Status.Terminated },
        stopped.blocks,
        stopped.loops,
      ),
    ).toBe("Run stopped where the doc says to stop");
    expect(docTagState(stopped.blocks.get("stop_here"), false, true)).toBe(
      "succeeded",
    );
    expect(
      docRunEndMessage({ status: Status.Failed }, failed.blocks, failed.loops),
    ).toBe("Run failed. See the red step.");
    expect(
      docRunEndMessage({ status: Status.Canceled }, plain.blocks, plain.loops),
    ).toBe("Run stopped");
    // A Stop step that itself failed is a failure, not the doc's intended stop.
    const brokenStop = project([
      item(row("open_site")),
      item(
        row("stop_here", { block_type: "terminate", status: Status.Failed }),
      ),
    ]);
    expect(docTagState(brokenStop.blocks.get("stop_here"), false, true)).toBe(
      "failed",
    );
    expect(
      docRunEndMessage(
        { status: Status.Failed },
        brokenStop.blocks,
        brokenStop.loops,
      ),
    ).toBe("Run failed. See the red step.");
    // The run can say "terminated" a poll before the Stop step's row finishes.
    const lagging = project([
      item(row("open_site")),
      item(
        row("stop_here", { block_type: "terminate", status: Status.Running }),
      ),
    ]);
    expect(docRunRowsSettled(lagging.blocks)).toBe(false);
    // A run can go final before its first timeline rows arrive; the ending waits for them.
    expect(docRunRowsSettled(new Map())).toBe(false);
    expect(docRunRowsSettled(stopped.blocks)).toBe(true);
  });
});

describe("stepsUnknownToRun authored identifiers", () => {
  it("treats a changed credential_id as an edited step but ignores server stamps", () => {
    const step = (credential: string, stamp: string) => ({
      blocks: [
        {
          label: "login",
          block_type: "login",
          credential_id: credential,
          output_parameter: { output_parameter_id: stamp, created_at: stamp },
        },
      ],
    });
    expect(stepsUnknownToRun(step("c1", "a"), step("c1", "b")).size).toBe(0);
    expect([...stepsUnknownToRun(step("c1", "a"), step("c2", "b"))]).toEqual([
      "login",
    ]);
  });
});
