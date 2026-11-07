import { describe, expect, it } from "vitest";

import { Status } from "@/api/types";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../../types/workflowRunTypes";
import {
  loopItemText,
  collectBlockRuns,
  collectLoopProgress,
  type DocBlockRuns,
  docLoopProgressText,
  docRunEndMessage,
  docRunRowsSettled,
  docTagState,
  ranAnotherVersion,
  settleRowsOfEndedRun,
  stepsUnknownToRun,
  type DocLoopProgress,
} from "./docRunState";
import { docProduced } from "./docStepProduced";
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
  it("counts a looped step's runs that returned a value, apart from those that returned nothing", () => {
    const code = (
      output: WorkflowRunBlock["output"],
      index: number,
      status: Status = Status.Completed,
    ) =>
      item(
        row("check_price", {
          block_type: "code",
          output,
          status,
          current_index: index,
        }),
      );
    const runs = new Map<string, DocBlockRuns>();
    collectBlockRuns(
      [
        item(row("each_book", { block_type: "for_loop" }), [
          code({ result: "under budget" }, 0),
          code(null, 1),
          code({ result: "under budget" }, 2),
          code(null, 3, Status.Skipped),
        ]),
      ],
      RUN,
      runs,
      (block) => docProduced(block) !== null,
    );
    expect(runs.get("check_price")).toEqual({
      count: 3,
      failed: 0,
      canceled: 0,
      withOutput: 2,
    });
  });

  it("counts a script fallback's two rows as one run, and a Task V2 step by its replaced row", () => {
    const runs = new Map<string, DocBlockRuns>();
    const task = (status: Status, extra: Partial<WorkflowRunBlock> = {}) =>
      row("open_site", {
        status,
        parent_workflow_run_block_id: "wfrb_parent",
        ...extra,
      });
    const v2Row = {
      ...item(row("inner_task")),
      task_v2_blocks: [row("research", { block_type: "task_v2" })],
    } as WorkflowRunTimelineItem;
    collectBlockRuns(
      [
        item(task(Status.Failed)),
        item(task(Status.Completed)),
        item(row("each_book", { block_type: "for_loop" }), [
          v2Row,
          item(row("research", { current_index: 1 })),
        ]),
      ],
      RUN,
      runs,
      () => false,
    );
    expect(runs.get("open_site")).toMatchObject({ count: 1, failed: 0 });
    expect(runs.get("research")?.count).toBe(2);
  });

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

    expect(progress.items.map((i) => [i.title, i.state])).toEqual([
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

  it("names a loop item in a few plain words and keeps its full value for the disclosure", () => {
    const book = { title: "A Light in the Attic", price: 51.77, id: "b_1" };
    expect(loopItemText(book, "{'title': 'A Light in the Attic'}")).toEqual({
      title: "A Light in the Attic",
      detail: JSON.stringify(book, null, 2),
    });
    const result = { task_id: "tsk_1", downloaded_file_urls: null };
    expect(loopItemText(result, null).title).toBeNull();
    expect(
      loopItemText(
        undefined,
        "{'task_id': 'tsk_1', 'downloaded_file_urls': None}",
      ),
    ).toEqual({
      title: null,
      detail: "{'task_id': 'tsk_1', 'downloaded_file_urls': None}",
    });
    expect(loopItemText("Travel", "Travel")).toEqual({
      title: "Travel",
      detail: null,
    });
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
    const ifStep = (branchId: string) => ({
      blocks: [
        {
          label: "check_price",
          block_type: "conditional",
          branch_conditions: [{ id: branchId, is_default: true }],
        },
      ],
    });
    // A save re-mints branch ids; the If itself did not change.
    expect(stepsUnknownToRun(ifStep("old"), ifStep("new")).size).toBe(0);
    expect([...stepsUnknownToRun(step("c1", "a"), step("c2", "b"))]).toEqual([
      "login",
    ]);
  });

  it("still sees an authored field named id as an edit", () => {
    const exportStep = (idType: string) => ({
      blocks: [
        {
          label: "export",
          block_type: "data_export",
          schema: { properties: { id: { type: idType } } },
        },
      ],
    });
    expect([
      ...stepsUnknownToRun(exportStep("integer"), exportStep("string")),
    ]).toEqual(["export"]);
  });
});

describe("stepsUnknownToRun implicit chain", () => {
  it("does not read the editor's filled-in next step as an edit", () => {
    const step = (label: string, next?: string) => ({
      label,
      block_type: "code",
      code: "pass",
      ...(next ? { next_block_label: next } : {}),
    });
    const first = { blocks: [step("a"), step("b"), step("c")] };
    const saved = { blocks: [step("a", "b"), step("b", "c"), step("c")] };
    expect(stepsUnknownToRun(first, saved).size).toBe(0);
  });
});

describe("stepsUnknownToRun across saves", () => {
  // Shaped like two stored versions of one agent: the first made outside the editor, the second its first Doc save.
  const version = (
    save: "first" | "editor",
    stamp: string,
    extraction: Record<string, unknown> = {},
  ) => ({
    parameters: [
      {
        key: "books",
        workflow_parameter_id: `wp_${stamp}`,
        workflow_id: stamp,
      },
    ],
    blocks: [
      {
        label: "loop_1",
        block_type: "for_loop",
        title: save === "first" ? "" : "loop_1",
        loop_over: {
          key: "books",
          parameter_type: "workflow",
          workflow_parameter_id: `wp_${stamp}`,
          workflow_id: stamp,
          created_at: stamp,
          modified_at: stamp,
        },
        loop_blocks: [
          {
            label: "extraction_1",
            block_type: "extraction",
            title: save === "first" ? "" : "extraction_1",
            url: save === "first" ? null : "",
            data_extraction_goal: "Title and price of each book",
            export_enabled: false,
            export_data_schema:
              save === "first"
                ? null
                : {
                    type: "array",
                    items: {
                      type: "object",
                      properties: { value: { type: "string" } },
                    },
                  },
            output_parameter: { output_parameter_id: `op_${stamp}` },
            ...extraction,
          },
          { label: "code_1", block_type: "code", code: "pass" },
        ],
      },
    ],
  });

  it("keeps every step's result after a save with no edits", () => {
    expect(
      stepsUnknownToRun(version("first", "a"), version("editor", "b")).size,
    ).toBe(0);
    expect(
      stepsUnknownToRun(version("editor", "b"), version("editor", "c")).size,
    ).toBe(0);
  });

  it("hides only the edited step, not the loop around it", () => {
    expect([
      ...stepsUnknownToRun(
        version("first", "a"),
        version("editor", "b", {
          data_extraction_goal: "Title and price of each book and its price",
        }),
      ),
    ]).toEqual(["extraction_1"]);
  });

  it("still sees an authored url or an export schema in use as an edit", () => {
    const withUrl = version("editor", "b", { url: "https://example.com" });
    expect([...stepsUnknownToRun(version("editor", "a"), withUrl)]).toEqual([
      "extraction_1",
    ]);
    const schemaField = version("editor", "b", {
      data_schema: { properties: { workflow_id: { type: "string" } } },
    });
    expect([...stepsUnknownToRun(version("editor", "a"), schemaField)]).toEqual(
      ["extraction_1"],
    );
    const exporting = version("editor", "b", { export_enabled: true });
    expect([...stepsUnknownToRun(version("first", "a"), exporting)]).toEqual([
      "extraction_1",
    ]);
  });
});

describe("a run that has ended", () => {
  // Stop pressed during item 3 of 3: the step on item 3 is still "running" in the rows.
  const stoppedLoop = (inner: Status) => [
    item(
      row("each_page", {
        block_type: "for_loop",
        loop_values: ["a", "b", "c"],
        status: inner,
      }),
      [2, 1, 0].map((index) =>
        item(
          row("open_page", {
            current_index: index,
            current_value: "abc"[index],
            status: index === 2 ? inner : Status.Completed,
          }),
        ),
      ),
    ),
  ];

  it("shows the item that was running at Stop as canceled, before and after the rows catch up", () => {
    for (const inner of [Status.Running, Status.Canceled]) {
      const timeline = settleRowsOfEndedRun(stoppedLoop(inner), {
        status: Status.Canceled,
      });
      const { blocks, loops } = project(timeline);
      expect(loops.get("each_page")!.items.map((i) => i.state)).toEqual([
        "succeeded",
        "succeeded",
        "canceled",
      ]);
      expect(blocks.get("open_page")!.status).toBe(Status.Canceled);
      const runs = new Map<string, DocBlockRuns>();
      collectBlockRuns(timeline, RUN, runs, () => false);
      expect(runs.get("open_page")).toMatchObject({ count: 3, canceled: 1 });
    }
  });

  it("reads a step the backend still has running on a failed run as that failure, not as stopped", () => {
    const timeline = settleRowsOfEndedRun(
      [item(row("open_site", { status: Status.Running }))],
      { status: Status.Failed, failure_reason: "Failed to navigate to url" },
    );
    const block = project(timeline).blocks.get("open_site")!;
    expect(block.status).toBe(Status.Failed);
    expect(block.failure_reason).toBe("Failed to navigate to url");
    expect(docTagState(block, false, true)).toBe("failed");
  });

  it("leaves a completed or terminated run's rows, and a lagging Stop step, as they are", () => {
    const timeline = [item(row("open_site", { status: Status.Running }))];
    for (const status of [Status.Completed, Status.Terminated])
      expect(settleRowsOfEndedRun(timeline, { status })).toBe(timeline);
    const stop = [
      item(
        row("stop_here", { block_type: "terminate", status: Status.Running }),
      ),
    ];
    const settled = settleRowsOfEndedRun(stop, { status: Status.TimedOut });
    expect(project(settled).blocks.get("stop_here")!.status).toBe(
      Status.Running,
    );
  });
});

describe("ranAnotherVersion", () => {
  const steps = (goal: string) => ({
    blocks: [{ label: "open_site", block_type: "goto_url", url: goal }],
  });
  const at = (version: number, goal: string) => ({
    version,
    workflow_definition: steps(goal),
  });
  const test = { copilotTest: true, proposalPending: false };
  const plain = { copilotTest: false, proposalPending: false };

  it("treats a rejected proposal's test run as another version than the saved agent", () => {
    // Copilot tested the proposal as v4, then it was rejected; the agent is still v3.
    expect(ranAnotherVersion(at(4, "proposal"), at(3, "saved"), test)).toBe(
      true,
    );
    // Its draft version is deleted once the run ends, whatever the run says.
    expect(
      ranAnotherVersion(
        { ...at(4, "proposal"), deleted_at: "2026-11-06T16:22:47" },
        at(3, "saved"),
        plain,
      ),
    ).toBe(true);
    expect(ranAnotherVersion(at(2, "older"), at(3, "saved"), plain)).toBe(true);
  });

  it("keeps a proposal's test while it is under review, a newer save, and a version with the same steps", () => {
    expect(
      ranAnotherVersion(at(4, "proposal"), at(3, "saved"), {
        copilotTest: true,
        proposalPending: true,
      }),
    ).toBe(false);
    expect(
      ranAnotherVersion(at(4, "saved elsewhere"), at(3, "saved"), plain),
    ).toBe(false);
    expect(ranAnotherVersion(at(3, "saved"), at(4, "saved"), test)).toBe(false);
    expect(ranAnotherVersion(at(3, "saved"), at(3, "saved"), plain)).toBe(
      false,
    );
  });
});
