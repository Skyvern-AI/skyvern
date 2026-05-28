import { describe, expect, test } from "vitest";

import { Status } from "@/api/types";
import {
  countActionsInTimeline,
  type ObserverThought,
  type WorkflowRunBlock,
  type WorkflowRunTimelineBlockItem,
  type WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import {
  aggregateIterationStatus,
  findActiveItem,
  findBlockSurroundingThought,
  findLastExecutedBlock,
  findRunningBlock,
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
