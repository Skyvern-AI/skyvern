import { describe, expect, test } from "vitest";

import { ActionsApiResponse, Status } from "@/api/types";
import {
  WorkflowRunBlock,
  WorkflowRunTimelineBlockItem,
  WorkflowRunTimelineItem,
} from "@/routes/workflows/types/workflowRunTypes";
import {
  buildActionIndex,
  buildBlockStatusMap,
  buildFilmstrip,
  runOutcomeFromStatus,
} from "./runProjections";

let idCounter = 0;
const uid = (prefix: string) => `${prefix}_${++idCounter}`;

function action(
  overrides: Partial<ActionsApiResponse> = {},
): ActionsApiResponse {
  return {
    action_id: uid("act"),
    action_type: "click",
    status: Status.Completed,
    task_id: null,
    step_id: "step_1",
    step_order: 0,
    action_order: 0,
    confidence_float: null,
    description: null,
    reasoning: null,
    intention: null,
    response: null,
    created_by: null,
    text: null,
    screenshot_artifact_id: null,
    ...overrides,
  };
}

function blockItem(
  block: Partial<WorkflowRunBlock>,
  children: WorkflowRunTimelineItem[] = [],
): WorkflowRunTimelineBlockItem {
  return {
    type: "block",
    thought: null,
    children,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    block: {
      workflow_run_block_id: uid("wrb"),
      workflow_run_id: "wr_1",
      parent_workflow_run_block_id: null,
      block_type: "task",
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
      ...block,
    },
  };
}

describe("runOutcomeFromStatus", () => {
  test("idle when there is no status", () => {
    expect(runOutcomeFromStatus(null)).toBe("idle");
    expect(runOutcomeFromStatus(undefined)).toBe("idle");
  });

  test("running for in-flight statuses", () => {
    expect(runOutcomeFromStatus(Status.Created)).toBe("running");
    expect(runOutcomeFromStatus(Status.Queued)).toBe("running");
    expect(runOutcomeFromStatus(Status.Running)).toBe("running");
    expect(runOutcomeFromStatus(Status.Paused)).toBe("running");
  });

  test("success only for completed", () => {
    expect(runOutcomeFromStatus(Status.Completed)).toBe("success");
  });

  test("failed for failure types and canceled", () => {
    expect(runOutcomeFromStatus(Status.Failed)).toBe("failed");
    expect(runOutcomeFromStatus(Status.Terminated)).toBe("failed");
    expect(runOutcomeFromStatus(Status.TimedOut)).toBe("failed");
    expect(runOutcomeFromStatus(Status.Canceled)).toBe("failed");
  });
});

describe("buildFilmstrip", () => {
  test("empty or undefined timeline yields no frames", () => {
    expect(buildFilmstrip(undefined)).toEqual([]);
    expect(buildFilmstrip([])).toEqual([]);
  });

  test("orders a block's actions oldest-first with a 1-based index", () => {
    // block.actions arrives newest-first (created_at desc); the strip reverses
    // it to oldest-first so it matches the run timeline tree.
    const frames = buildFilmstrip([
      blockItem({
        label: "block_1",
        actions: [
          action({ action_id: "newest", action_type: "click" }),
          action({ action_id: "oldest", action_type: "goto_url" }),
        ],
      }),
    ]);
    expect(frames.map((f) => f.id)).toEqual(["oldest", "newest"]);
    expect(frames.map((f) => f.index)).toEqual([1, 2]);
    expect(frames.map((f) => f.isBlockStart)).toEqual([true, false]);
  });

  test("marks block boundaries across a multi-block run", () => {
    const frames = buildFilmstrip([
      blockItem({
        workflow_run_block_id: "b1",
        label: "block_1",
        actions: [action({ action_id: "a1" })],
      }),
      blockItem({
        workflow_run_block_id: "b2",
        label: "block_2",
        actions: [action({ action_id: "a2" }), action({ action_id: "a3" })],
      }),
    ]);
    expect(frames.map((f) => f.isBlockStart)).toEqual([true, true, false]);
    expect(frames.map((f) => f.index)).toEqual([1, 2, 3]);
    expect(frames.map((f) => f.blockLabel)).toEqual([
      "block_1",
      "block_2",
      "block_2",
    ]);
  });

  test("recurses into loop children", () => {
    const frames = buildFilmstrip([
      blockItem({ block_type: "for_loop", label: "loop", actions: null }, [
        blockItem({
          workflow_run_block_id: "i1",
          label: "inner",
          actions: [action({ action_id: "a1" })],
        }),
        blockItem({
          workflow_run_block_id: "i2",
          label: "inner",
          actions: [action({ action_id: "a2" })],
        }),
      ]),
    ]);
    expect(frames.map((f) => f.id)).toEqual(["a1", "a2"]);
  });

  test("label prefers intention, then description, then readable action type", () => {
    // Actions are passed newest-first; the strip renders them oldest-first.
    const frames = buildFilmstrip([
      blockItem({
        label: "b",
        actions: [
          action({ intention: "Submit search", description: "x" }),
          action({ intention: null, description: "Dismiss cookie" }),
          action({
            intention: null,
            description: null,
            reasoning: null,
            action_type: "scroll",
          }),
        ],
      }),
    ]);
    expect(frames.map((f) => f.label)).toEqual([
      "Scroll",
      "Dismiss cookie",
      "Submit search",
    ]);
  });
});

describe("buildBlockStatusMap", () => {
  test("keys by label with status, action count, and failure reason", () => {
    const map = buildBlockStatusMap([
      blockItem({
        label: "block_1",
        status: Status.Completed,
        actions: [action(), action()],
      }),
      blockItem({
        label: "block_2",
        status: Status.Failed,
        failure_reason: "boom",
        actions: [action()],
      }),
    ]);
    expect(map["block_1"]).toMatchObject({
      status: Status.Completed,
      actionCount: 2,
    });
    expect(map["block_2"]).toMatchObject({
      status: Status.Failed,
      failureReason: "boom",
      actionCount: 1,
    });
  });

  test("latest occurrence wins for looped block labels", () => {
    const map = buildBlockStatusMap([
      blockItem({ block_type: "for_loop", label: "loop", actions: null }, [
        blockItem({
          label: "inner",
          status: Status.Completed,
          actions: [action()],
        }),
        blockItem({
          label: "inner",
          status: Status.Failed,
          actions: [action(), action()],
        }),
      ]),
    ]);
    expect(map["inner"]).toMatchObject({
      status: Status.Failed,
      actionCount: 2,
    });
  });
});

describe("buildActionIndex", () => {
  test("maps action_id to the action across blocks and loop children", () => {
    const a1 = action({ action_id: "a1" });
    const a2 = action({ action_id: "a2" });
    const index = buildActionIndex([
      blockItem({ label: "block_1", actions: [a1] }),
      blockItem({ block_type: "for_loop", label: "loop", actions: null }, [
        blockItem({ label: "inner", actions: [a2] }),
      ]),
    ]);
    expect(index.get("a1")).toBe(a1);
    expect(index.get("a2")).toBe(a2);
    expect(index.size).toBe(2);
  });

  test("empty timeline yields an empty index", () => {
    expect(buildActionIndex(undefined).size).toBe(0);
  });
});
