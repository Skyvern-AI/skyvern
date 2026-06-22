import { describe, expect, test } from "vitest";

import { Status } from "@/api/types";
import {
  countActionsInTimeline,
  type ObserverThought,
  type WorkflowRunBlock,
  type WorkflowRunTimelineBlockItem,
  type WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import type { WorkflowBlock } from "../types/workflowTypes";
import {
  aggregateIterationStatus,
  classifyUnexecutedDefinedBlocks,
  findActiveItem,
  findBlockSurroundingThought,
  findLastExecutedBlock,
  findRunningBlock,
  findTimelineBlock,
  flattenTimelineChronologically,
  resolveScreenshotBlockId,
} from "./workflowTimelineUtils";

function buildBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_default",
    workflow_run_id: "wr_default",
    parent_workflow_run_block_id: null,
    block_type: "http_request",
    label: null,
    description: null,
    title: null,
    status: Status.Completed,
    failure_reason: null,
    output: null,
    continue_on_failure: false,
    task_id: null,
    url: null,
    navigation_goal: null,
    navigation_payload: null,
    data_extraction_goal: null,
    data_schema: null,
    terminate_criterion: null,
    complete_criterion: null,
    include_action_history_in_verification: null,
    engine: null,
    actions: null,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    duration: null,
    loop_values: null,
    current_value: null,
    current_index: null,
    ...overrides,
  };
}

function buildBlockItem(
  block: WorkflowRunBlock,
  children: Array<WorkflowRunTimelineItem> = [],
): WorkflowRunTimelineBlockItem {
  return {
    type: "block",
    block,
    children,
    thought: null,
    created_at: block.created_at,
    modified_at: block.modified_at,
  };
}

function buildThoughtItem(
  overrides: Partial<ObserverThought> = {},
): WorkflowRunTimelineItem {
  const thought = {
    thought_id: "thought_default",
    user_input: null,
    observation: null,
    thought: "Thinking",
    answer: null,
    created_at: "2026-01-01T00:00:02Z",
    modified_at: "2026-01-01T00:00:02Z",
    ...overrides,
  };
  return {
    type: "thought",
    block: null,
    children: [],
    thought,
    created_at: thought.created_at,
    modified_at: thought.modified_at,
  };
}

describe("findRunningBlock", () => {
  test("returns null when no block is running", () => {
    const timeline = [buildBlockItem(buildBlock({ status: Status.Completed }))];
    expect(findRunningBlock(timeline)).toBeNull();
  });

  test("returns the deepest running block (leaf preferred over container)", () => {
    // outer for_loop is running because its child is running, and so is the
    // nested http_request leaf. We want the leaf, not the loop.
    const leaf = buildBlock({
      workflow_run_block_id: "wrb_leaf",
      block_type: "http_request",
      status: Status.Running,
    });
    const conditional = buildBlock({
      workflow_run_block_id: "wrb_cond",
      block_type: "conditional",
      status: Status.Running,
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
      status: Status.Running,
    });

    const timeline: Array<WorkflowRunTimelineItem> = [
      buildBlockItem(loop, [
        buildBlockItem(conditional, [buildBlockItem(leaf)]),
      ]),
    ];

    expect(findRunningBlock(timeline)?.workflow_run_block_id).toBe("wrb_leaf");
  });
});

describe("findBlockSurroundingThought", () => {
  test("falls back to an in-progress block whose modified_at lags the thought", () => {
    const thought = buildThoughtItem({
      thought_id: "thought_live",
      created_at: "2026-01-01T00:00:10Z",
      modified_at: "2026-01-01T00:00:10Z",
    });
    const block = buildBlock({
      workflow_run_block_id: "wrb_running",
      status: Status.Running,
      created_at: "2026-01-01T00:00:00Z",
      modified_at: "2026-01-01T00:00:05Z",
    });

    expect(
      findBlockSurroundingThought(
        [buildBlockItem(block), thought],
        "thought_live",
      )?.workflow_run_block_id,
    ).toBe("wrb_running");
  });

  test("uses deepest matching block as the time-range fallback tie-breaker", () => {
    const thought = buildThoughtItem({
      thought_id: "thought_nested",
      created_at: "2026-01-01T00:00:05Z",
      modified_at: "2026-01-01T00:00:05Z",
    });
    const outer = buildBlock({
      workflow_run_block_id: "wrb_outer",
      block_type: "for_loop",
      created_at: "2026-01-01T00:00:00Z",
      modified_at: "2026-01-01T00:00:10Z",
    });
    const inner = buildBlock({
      workflow_run_block_id: "wrb_inner",
      created_at: "2026-01-01T00:00:00Z",
      modified_at: "2026-01-01T00:00:10Z",
    });

    expect(
      findBlockSurroundingThought(
        [buildBlockItem(outer, [buildBlockItem(inner)]), thought],
        "thought_nested",
      )?.workflow_run_block_id,
    ).toBe("wrb_inner");
  });
});

describe("countActionsInTimeline", () => {
  test("counts actions on conditional blocks", () => {
    const conditional = buildBlock({
      workflow_run_block_id: "wrb_cond",
      block_type: "conditional",
      actions: [
        { action_id: "act_extract" },
      ] as unknown as WorkflowRunBlock["actions"],
    });

    expect(countActionsInTimeline([buildBlockItem(conditional)])).toBe(1);
  });
});

describe("findLastExecutedBlock", () => {
  test("returns null when there are no terminal leaf blocks", () => {
    const timeline = [buildBlockItem(buildBlock({ status: Status.Running }))];
    expect(findLastExecutedBlock(timeline)).toBeNull();
  });

  test("filters to leaves so an outer container doesn't shadow the actual last step", () => {
    // Container's modified_at is later than its child's (containers close
    // after their children). The helper should still surface the leaf.
    const leaf = buildBlock({
      workflow_run_block_id: "wrb_leaf",
      status: Status.Completed,
      modified_at: "2026-01-01T00:00:01Z",
    });
    const container = buildBlock({
      workflow_run_block_id: "wrb_container",
      block_type: "for_loop",
      status: Status.Completed,
      // modified after the leaf finished
      modified_at: "2026-01-01T00:00:05Z",
    });

    const timeline: Array<WorkflowRunTimelineItem> = [
      buildBlockItem(container, [buildBlockItem(leaf)]),
    ];

    expect(findLastExecutedBlock(timeline)?.workflow_run_block_id).toBe(
      "wrb_leaf",
    );
  });

  test("returns the most recently modified leaf when there are several", () => {
    const earlier = buildBlock({
      workflow_run_block_id: "wrb_earlier",
      status: Status.Completed,
      modified_at: "2026-01-01T00:00:01Z",
    });
    const later = buildBlock({
      workflow_run_block_id: "wrb_later",
      status: Status.Completed,
      modified_at: "2026-01-01T00:00:09Z",
    });

    const timeline: Array<WorkflowRunTimelineItem> = [
      buildBlockItem(earlier),
      buildBlockItem(later),
    ];

    expect(findLastExecutedBlock(timeline)?.workflow_run_block_id).toBe(
      "wrb_later",
    );
  });

  test("ignores running blocks (only terminal states count)", () => {
    const completed = buildBlock({
      workflow_run_block_id: "wrb_done",
      status: Status.Completed,
      modified_at: "2026-01-01T00:00:01Z",
    });
    const running = buildBlock({
      workflow_run_block_id: "wrb_running",
      status: Status.Running,
      // running's modified_at is later, but we still want completed
      modified_at: "2026-01-01T00:00:09Z",
    });

    const timeline: Array<WorkflowRunTimelineItem> = [
      buildBlockItem(running),
      buildBlockItem(completed),
    ];

    expect(findLastExecutedBlock(timeline)?.workflow_run_block_id).toBe(
      "wrb_done",
    );
  });

  test("ignores leaves in non-terminal pre-execution states", () => {
    const completed = buildBlock({
      workflow_run_block_id: "wrb_done",
      status: Status.Completed,
      modified_at: "2026-01-01T00:00:01Z",
    });
    const queued = buildBlock({
      workflow_run_block_id: "wrb_queued",
      status: Status.Queued,
      // queued's modified_at is later, but it hasn't executed yet
      modified_at: "2026-01-01T00:00:09Z",
    });
    const created = buildBlock({
      workflow_run_block_id: "wrb_created",
      status: Status.Created,
      modified_at: "2026-01-01T00:00:08Z",
    });
    const paused = buildBlock({
      workflow_run_block_id: "wrb_paused",
      status: Status.Paused,
      modified_at: "2026-01-01T00:00:07Z",
    });

    const timeline: Array<WorkflowRunTimelineItem> = [
      buildBlockItem(queued),
      buildBlockItem(created),
      buildBlockItem(paused),
      buildBlockItem(completed),
    ];

    expect(findLastExecutedBlock(timeline)?.workflow_run_block_id).toBe(
      "wrb_done",
    );
  });
});

describe("findActiveItem default selection", () => {
  test("returns 'stream' when no active param and the run is still in progress", () => {
    const timeline = [buildBlockItem(buildBlock({ status: Status.Running }))];
    expect(findActiveItem(timeline, null, false)).toBe("stream");
  });

  test("returns the last-executed leaf when finalized and no active param", () => {
    const leaf = buildBlock({
      workflow_run_block_id: "wrb_leaf",
      status: Status.Completed,
      modified_at: "2026-01-01T00:00:01Z",
    });
    const container = buildBlock({
      workflow_run_block_id: "wrb_container",
      block_type: "for_loop",
      status: Status.Completed,
      modified_at: "2026-01-01T00:00:05Z",
    });

    const timeline: Array<WorkflowRunTimelineItem> = [
      buildBlockItem(container, [buildBlockItem(leaf)]),
    ];

    const result = findActiveItem(timeline, null, true);
    // result is the leaf block itself (no actions to prefer)
    expect(result).not.toBeNull();
    if (
      result &&
      typeof result === "object" &&
      "workflow_run_block_id" in result
    ) {
      expect(result.workflow_run_block_id).toBe("wrb_leaf");
    }
  });

  test("explicit target id wins over default selection", () => {
    const a = buildBlock({
      workflow_run_block_id: "wrb_a",
      modified_at: "2026-01-01T00:00:01Z",
    });
    const b = buildBlock({
      workflow_run_block_id: "wrb_b",
      modified_at: "2026-01-01T00:00:09Z",
    });

    const timeline: Array<WorkflowRunTimelineItem> = [
      buildBlockItem(a),
      buildBlockItem(b),
    ];

    const result = findActiveItem(timeline, "wrb_a", true);
    expect(result).not.toBeNull();
    if (
      result &&
      typeof result === "object" &&
      "workflow_run_block_id" in result
    ) {
      expect(result.workflow_run_block_id).toBe("wrb_a");
    }
  });
});

describe("aggregateIterationStatus", () => {
  test("returns null for an empty input", () => {
    expect(aggregateIterationStatus([])).toBeNull();
  });

  test("returns Completed when every child is Completed", () => {
    const items = [
      buildBlockItem(buildBlock({ status: Status.Completed })),
      buildBlockItem(buildBlock({ status: Status.Completed })),
    ];
    expect(aggregateIterationStatus(items)).toBe(Status.Completed);
  });

  test("returns Completed when children mix Completed and Skipped", () => {
    const items = [
      buildBlockItem(buildBlock({ status: Status.Completed })),
      buildBlockItem(buildBlock({ status: Status.Skipped })),
    ];
    expect(aggregateIterationStatus(items)).toBe(Status.Completed);
  });

  test("returns Failed when any child is Failed/Terminated/TimedOut/Canceled", () => {
    const items = [
      buildBlockItem(buildBlock({ status: Status.Completed })),
      buildBlockItem(buildBlock({ status: Status.Terminated })),
    ];
    expect(aggregateIterationStatus(items)).toBe(Status.Failed);
  });

  test("returns Running when any child is Running and nothing failed", () => {
    const items = [
      buildBlockItem(buildBlock({ status: Status.Completed })),
      buildBlockItem(buildBlock({ status: Status.Running })),
    ];
    expect(aggregateIterationStatus(items)).toBe(Status.Running);
  });

  test("prefers Failed over Running when both are present", () => {
    const items = [
      buildBlockItem(buildBlock({ status: Status.Running })),
      buildBlockItem(buildBlock({ status: Status.Failed })),
    ];
    expect(aggregateIterationStatus(items)).toBe(Status.Failed);
  });

  test("returns null when every child is in a pre-execution state", () => {
    const items = [
      buildBlockItem(buildBlock({ status: Status.Created })),
      buildBlockItem(buildBlock({ status: Status.Queued })),
      buildBlockItem(buildBlock({ status: Status.Paused })),
    ];
    expect(aggregateIterationStatus(items)).toBeNull();
  });

  test("returns null when children mix Completed with pending — does not claim Completed prematurely", () => {
    const items = [
      buildBlockItem(buildBlock({ status: Status.Completed })),
      buildBlockItem(buildBlock({ status: Status.Queued })),
    ];
    expect(aggregateIterationStatus(items)).toBeNull();
  });

  test("returns null when a child status is null", () => {
    const items = [buildBlockItem(buildBlock({ status: null }))];
    expect(aggregateIterationStatus(items)).toBeNull();
  });

  test("walks nested children", () => {
    const items = [
      buildBlockItem(buildBlock({ status: Status.Completed }), [
        buildBlockItem(buildBlock({ status: Status.Failed })),
      ]),
    ];
    expect(aggregateIterationStatus(items)).toBe(Status.Failed);
  });
});

describe("flattenTimelineChronologically", () => {
  function rootIds(items: Array<WorkflowRunTimelineItem>): Array<string> {
    return items.map((item) =>
      item.type === "block" ? item.block.workflow_run_block_id : "thought",
    );
  }

  test("hoists conditional branch chains so visible order matches execution order", () => {
    // Regression: a conditional's branch blocks executed AFTER a root-level
    // loop, but tree rendering printed them above it (the terminated block
    // looked like it ran before the loop).
    //   root  block_2 conditional      07:16
    //   root  block_5 for_loop         07:19 (children 07:19–07:27)
    //   child block_8 (parent block_2) 07:29
    //   child block_12 (parent block_8) 07:39, terminated
    const conditional = buildBlock({
      workflow_run_block_id: "wrb_block_2",
      block_type: "conditional",
      created_at: "2026-06-10T07:16:29Z",
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_block_5",
      block_type: "for_loop",
      created_at: "2026-06-10T07:19:06Z",
    });
    const loopChild = buildBlock({
      workflow_run_block_id: "wrb_loop_child",
      parent_workflow_run_block_id: "wrb_block_5",
      created_at: "2026-06-10T07:19:11Z",
      current_index: 0,
    });
    const branchConditional = buildBlock({
      workflow_run_block_id: "wrb_block_8",
      block_type: "conditional",
      parent_workflow_run_block_id: "wrb_block_2",
      created_at: "2026-06-10T07:29:32Z",
    });
    const terminated = buildBlock({
      workflow_run_block_id: "wrb_block_12",
      block_type: "navigation",
      status: Status.Terminated,
      parent_workflow_run_block_id: "wrb_block_8",
      created_at: "2026-06-10T07:39:31Z",
    });

    const flattened = flattenTimelineChronologically([
      buildBlockItem(conditional, [
        buildBlockItem(branchConditional, [buildBlockItem(terminated)]),
      ]),
      buildBlockItem(loop, [buildBlockItem(loopChild)]),
    ]);

    expect(rootIds(flattened)).toEqual([
      "wrb_block_2",
      "wrb_block_5",
      "wrb_block_8",
      "wrb_block_12",
    ]);
    // The loop keeps its iteration children nested; hoisted conditional rows
    // carry none.
    const loopRow = flattened.find(
      (item) =>
        item.type === "block" &&
        item.block.workflow_run_block_id === "wrb_block_5",
    );
    expect(rootIds(loopRow?.children ?? [])).toEqual(["wrb_loop_child"]);
    const conditionalRow = flattened.find(
      (item) =>
        item.type === "block" &&
        item.block.workflow_run_block_id === "wrb_block_2",
    );
    expect(conditionalRow?.children).toEqual([]);
  });

  test("interleaves a conditional's scoped blocks around a root loop by created_at", () => {
    // The same conditional parents blocks both before (07:17) and after
    // (07:28) the root loop ran (07:19) — they must straddle the loop row.
    const conditional = buildBlock({
      workflow_run_block_id: "wrb_cond",
      block_type: "conditional",
      created_at: "2026-06-10T07:16:29Z",
    });
    const beforeLoop = buildBlock({
      workflow_run_block_id: "wrb_before_loop",
      parent_workflow_run_block_id: "wrb_cond",
      created_at: "2026-06-10T07:17:40Z",
    });
    const afterLoop = buildBlock({
      workflow_run_block_id: "wrb_after_loop",
      block_type: "wait",
      parent_workflow_run_block_id: "wrb_cond",
      created_at: "2026-06-10T07:28:55Z",
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
      created_at: "2026-06-10T07:19:06Z",
    });

    const flattened = flattenTimelineChronologically([
      buildBlockItem(conditional, [
        buildBlockItem(beforeLoop),
        buildBlockItem(afterLoop),
      ]),
      buildBlockItem(loop),
    ]);

    expect(rootIds(flattened)).toEqual([
      "wrb_cond",
      "wrb_before_loop",
      "wrb_loop",
      "wrb_after_loop",
    ]);
  });

  test("keeps loop children nested and sorts them ascending by created_at", () => {
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
      created_at: "2026-06-10T07:00:00Z",
    });
    const later = buildBlock({
      workflow_run_block_id: "wrb_later",
      parent_workflow_run_block_id: "wrb_loop",
      created_at: "2026-06-10T07:05:00Z",
      current_index: 1,
    });
    const earlier = buildBlock({
      workflow_run_block_id: "wrb_earlier",
      parent_workflow_run_block_id: "wrb_loop",
      created_at: "2026-06-10T07:01:00Z",
      current_index: 0,
    });

    const flattened = flattenTimelineChronologically([
      buildBlockItem(loop, [buildBlockItem(later), buildBlockItem(earlier)]),
    ]);

    expect(rootIds(flattened)).toEqual(["wrb_loop"]);
    expect(rootIds(flattened[0]!.children)).toEqual([
      "wrb_earlier",
      "wrb_later",
    ]);
  });

  test("hoists a conditional's branch chain inside a loop into the loop's children", () => {
    // Inside a loop iteration, branch targets are parented to the in-loop
    // conditional. They keep their current_index, so hoisting them up to the
    // loop level preserves iteration grouping.
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
      created_at: "2026-06-10T07:00:00Z",
    });
    const inLoopConditional = buildBlock({
      workflow_run_block_id: "wrb_in_loop_cond",
      block_type: "conditional",
      parent_workflow_run_block_id: "wrb_loop",
      created_at: "2026-06-10T07:01:00Z",
      current_index: 0,
    });
    const branchTask = buildBlock({
      workflow_run_block_id: "wrb_branch_task",
      parent_workflow_run_block_id: "wrb_in_loop_cond",
      created_at: "2026-06-10T07:02:00Z",
      current_index: 0,
    });

    const flattened = flattenTimelineChronologically([
      buildBlockItem(loop, [
        buildBlockItem(inLoopConditional, [buildBlockItem(branchTask)]),
      ]),
    ]);

    expect(rootIds(flattened)).toEqual(["wrb_loop"]);
    expect(rootIds(flattened[0]!.children)).toEqual([
      "wrb_in_loop_cond",
      "wrb_branch_task",
    ]);
    const hoistedBranchTask = flattened[0]!.children[1]!;
    expect(
      hoistedBranchTask.type === "block" &&
        hoistedBranchTask.block.current_index,
    ).toBe(0);
  });

  test("sorts thought items among blocks chronologically", () => {
    const thought = buildThoughtItem({
      thought_id: "thought_mid",
      created_at: "2026-06-10T07:01:00Z",
    });
    const first = buildBlock({
      workflow_run_block_id: "wrb_first",
      created_at: "2026-06-10T07:00:00Z",
    });
    const last = buildBlock({
      workflow_run_block_id: "wrb_last",
      created_at: "2026-06-10T07:02:00Z",
    });

    const flattened = flattenTimelineChronologically([
      buildBlockItem(last),
      thought,
      buildBlockItem(first),
    ]);

    expect(rootIds(flattened)).toEqual(["wrb_first", "thought", "wrb_last"]);
  });
});

describe("classifyUnexecutedDefinedBlocks", () => {
  function buildDefinedBlock(
    overrides: Partial<{
      label: string;
      block_type: string;
      next_block_label: string | null;
      branch_conditions: Array<{
        id: string;
        next_block_label: string | null;
        is_default: boolean;
      }>;
    }>,
  ): WorkflowBlock {
    return {
      label: "defined_default",
      block_type: "navigation",
      next_block_label: null,
      ...overrides,
    } as unknown as WorkflowBlock;
  }

  function reasonsByLabel(
    result: ReturnType<typeof classifyUnexecutedDefinedBlocks>,
  ): Record<string, string> {
    return Object.fromEntries(result.map((r) => [r.block.label, r.reason]));
  }

  test("labels a not-taken branch chain as branch_not_taken using runtime evaluations", () => {
    const defined = [
      buildDefinedBlock({
        label: "cond",
        block_type: "conditional",
        branch_conditions: [
          { id: "br_a", next_block_label: "block_a", is_default: false },
          { id: "br_b", next_block_label: "block_b", is_default: true },
        ],
      }),
      buildDefinedBlock({ label: "block_a", next_block_label: null }),
      buildDefinedBlock({ label: "block_b", next_block_label: "block_b2" }),
      buildDefinedBlock({ label: "block_b2", next_block_label: null }),
    ];
    const conditional = buildBlock({
      workflow_run_block_id: "wrb_cond",
      block_type: "conditional",
      label: "cond",
      output: {
        evaluations: [
          { is_matched: true, next_block_label: "block_a" },
          { is_matched: false, next_block_label: "block_b" },
        ],
      } as WorkflowRunBlock["output"],
    });
    const taken = buildBlock({
      workflow_run_block_id: "wrb_a",
      label: "block_a",
      parent_workflow_run_block_id: "wrb_cond",
    });

    const result = classifyUnexecutedDefinedBlocks(defined, [
      buildBlockItem(conditional, [buildBlockItem(taken)]),
    ]);

    expect(reasonsByLabel(result)).toEqual({
      block_b: "branch_not_taken",
      block_b2: "branch_not_taken",
    });
  });

  test("labels an unexecuted block on the TAKEN branch as not_reached (run ended first)", () => {
    const defined = [
      buildDefinedBlock({
        label: "cond",
        block_type: "conditional",
        branch_conditions: [
          { id: "br_a", next_block_label: "block_a", is_default: false },
          { id: "br_b", next_block_label: "block_b", is_default: true },
        ],
      }),
      buildDefinedBlock({ label: "block_a", next_block_label: "block_a2" }),
      buildDefinedBlock({ label: "block_a2", next_block_label: null }),
      buildDefinedBlock({ label: "block_b", next_block_label: null }),
    ];
    const conditional = buildBlock({
      workflow_run_block_id: "wrb_cond",
      block_type: "conditional",
      label: "cond",
      output: {
        evaluations: [
          { is_matched: true, next_block_label: "block_a" },
          { is_matched: false, next_block_label: "block_b" },
        ],
      } as WorkflowRunBlock["output"],
    });
    const taken = buildBlock({
      workflow_run_block_id: "wrb_a",
      label: "block_a",
      status: Status.Terminated,
      parent_workflow_run_block_id: "wrb_cond",
    });

    const result = classifyUnexecutedDefinedBlocks(defined, [
      buildBlockItem(conditional, [buildBlockItem(taken)]),
    ]);

    // block_a2 was on the taken path but the run terminated at block_a.
    expect(reasonsByLabel(result)).toEqual({
      block_a2: "not_reached",
      block_b: "branch_not_taken",
    });
  });

  test("labels everything not_reached when the conditional itself never executed", () => {
    const defined = [
      buildDefinedBlock({ label: "first", next_block_label: "cond" }),
      buildDefinedBlock({
        label: "cond",
        block_type: "conditional",
        branch_conditions: [
          { id: "br_a", next_block_label: "block_a", is_default: false },
        ],
      }),
      buildDefinedBlock({ label: "block_a", next_block_label: null }),
    ];
    const first = buildBlock({
      workflow_run_block_id: "wrb_first",
      label: "first",
      status: Status.Terminated,
    });

    const result = classifyUnexecutedDefinedBlocks(defined, [
      buildBlockItem(first),
    ]);

    expect(reasonsByLabel(result)).toEqual({
      cond: "not_reached",
      block_a: "not_reached",
    });
  });

  test("descends into an unexecuted inner conditional inside a not-taken branch", () => {
    const defined = [
      buildDefinedBlock({
        label: "outer",
        block_type: "conditional",
        branch_conditions: [
          { id: "br_a", next_block_label: "block_a", is_default: false },
          { id: "br_inner", next_block_label: "inner", is_default: true },
        ],
      }),
      buildDefinedBlock({ label: "block_a", next_block_label: null }),
      buildDefinedBlock({
        label: "inner",
        block_type: "conditional",
        next_block_label: "inner_merge",
        branch_conditions: [
          { id: "br_x", next_block_label: "block_x", is_default: false },
        ],
      }),
      buildDefinedBlock({ label: "block_x", next_block_label: null }),
      buildDefinedBlock({ label: "inner_merge", next_block_label: null }),
    ];
    const outer = buildBlock({
      workflow_run_block_id: "wrb_outer",
      block_type: "conditional",
      label: "outer",
      output: {
        evaluations: [
          { is_matched: true, next_block_label: "block_a" },
          { is_matched: false, next_block_label: "inner" },
        ],
      } as WorkflowRunBlock["output"],
    });
    const taken = buildBlock({
      workflow_run_block_id: "wrb_a",
      label: "block_a",
      parent_workflow_run_block_id: "wrb_outer",
    });

    const result = classifyUnexecutedDefinedBlocks(defined, [
      buildBlockItem(outer, [buildBlockItem(taken)]),
    ]);

    expect(reasonsByLabel(result)).toEqual({
      inner: "branch_not_taken",
      block_x: "branch_not_taken",
      inner_merge: "branch_not_taken",
    });
  });

  test("falls back to executed_branch_next_block when output has no evaluations", () => {
    const defined = [
      buildDefinedBlock({
        label: "cond",
        block_type: "conditional",
        branch_conditions: [
          { id: "br_a", next_block_label: "block_a", is_default: false },
          { id: "br_b", next_block_label: "block_b", is_default: true },
        ],
      }),
      buildDefinedBlock({ label: "block_a", next_block_label: null }),
      buildDefinedBlock({ label: "block_b", next_block_label: null }),
    ];
    const conditional = buildBlock({
      workflow_run_block_id: "wrb_cond",
      block_type: "conditional",
      label: "cond",
      executed_branch_next_block: "block_a",
    });
    const taken = buildBlock({
      workflow_run_block_id: "wrb_a",
      label: "block_a",
      parent_workflow_run_block_id: "wrb_cond",
    });

    const result = classifyUnexecutedDefinedBlocks(defined, [
      buildBlockItem(conditional, [buildBlockItem(taken)]),
    ]);

    expect(reasonsByLabel(result)).toEqual({ block_b: "branch_not_taken" });
  });

  test("stays conservative (not_reached) when the taken branch is unknowable", () => {
    const defined = [
      buildDefinedBlock({
        label: "cond",
        block_type: "conditional",
        branch_conditions: [
          { id: "br_a", next_block_label: "block_a", is_default: false },
          { id: "br_b", next_block_label: "block_b", is_default: true },
        ],
      }),
      buildDefinedBlock({ label: "block_a", next_block_label: null }),
      buildDefinedBlock({ label: "block_b", next_block_label: null }),
    ];
    // Legacy run: no evaluations in output, no executed_branch_next_block.
    const conditional = buildBlock({
      workflow_run_block_id: "wrb_cond",
      block_type: "conditional",
      label: "cond",
    });
    const taken = buildBlock({
      workflow_run_block_id: "wrb_a",
      label: "block_a",
      parent_workflow_run_block_id: "wrb_cond",
    });

    const result = classifyUnexecutedDefinedBlocks(defined, [
      buildBlockItem(conditional, [buildBlockItem(taken)]),
    ]);

    expect(reasonsByLabel(result)).toEqual({ block_b: "not_reached" });
  });
});

describe("findTimelineBlock", () => {
  test("returns a nested leaf block by id so its real type is available", () => {
    const codeLeaf = buildBlock({
      workflow_run_block_id: "wrb_code_leaf",
      block_type: "code",
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
    });
    const timeline = [buildBlockItem(loop, [buildBlockItem(codeLeaf)])];
    expect(findTimelineBlock(timeline, "wrb_code_leaf")?.block_type).toBe(
      "code",
    );
  });

  test("returns null for an unknown id", () => {
    const leaf = buildBlock({ workflow_run_block_id: "wrb_leaf" });
    expect(findTimelineBlock([buildBlockItem(leaf)], "wrb_missing")).toBeNull();
  });
});

describe("resolveScreenshotBlockId", () => {
  test("returns the block's own id when it isn't a container", () => {
    const leaf = buildBlock({
      workflow_run_block_id: "wrb_leaf",
      block_type: "http_request",
    });
    expect(resolveScreenshotBlockId([buildBlockItem(leaf)], leaf)).toBe(
      "wrb_leaf",
    );
  });

  test("falls back to the newest leaf when no iteration index is provided", () => {
    // for_loop with two iterations; children are DESC-sorted by recency.
    // iteration 1 (newest) appears first; iteration 0 last.
    const newest = buildBlock({
      workflow_run_block_id: "wrb_iter1_leaf",
      block_type: "http_request",
      current_index: 1,
    });
    const oldest = buildBlock({
      workflow_run_block_id: "wrb_iter0_leaf",
      block_type: "http_request",
      current_index: 0,
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
    });
    const timeline = [
      buildBlockItem(loop, [buildBlockItem(newest), buildBlockItem(oldest)]),
    ];
    expect(resolveScreenshotBlockId(timeline, loop)).toBe("wrb_iter1_leaf");
  });

  test("scopes the leaf walk to the requested iteration's children", () => {
    const newest = buildBlock({
      workflow_run_block_id: "wrb_iter1_leaf",
      block_type: "http_request",
      current_index: 1,
    });
    const oldest = buildBlock({
      workflow_run_block_id: "wrb_iter0_leaf",
      block_type: "http_request",
      current_index: 0,
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
    });
    const timeline = [
      buildBlockItem(loop, [buildBlockItem(newest), buildBlockItem(oldest)]),
    ];
    // Asking for iteration 0 should NOT surface the iteration-1 leaf.
    expect(resolveScreenshotBlockId(timeline, loop, 0)).toBe("wrb_iter0_leaf");
    expect(resolveScreenshotBlockId(timeline, loop, 1)).toBe("wrb_iter1_leaf");
  });

  test("falls through to the newest leaf when the requested iteration has no children", () => {
    // URL says iteration=99 but the loop only ran two iterations.
    const newest = buildBlock({
      workflow_run_block_id: "wrb_iter1_leaf",
      block_type: "http_request",
      current_index: 1,
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
    });
    const timeline = [buildBlockItem(loop, [buildBlockItem(newest)])];
    expect(resolveScreenshotBlockId(timeline, loop, 99)).toBe("wrb_iter1_leaf");
  });
});
