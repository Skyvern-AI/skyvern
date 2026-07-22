import { describe, expect, test } from "vitest";

import {
  ActionsApiResponse,
  Status,
  TaskV2,
  type WorkflowRunStatusApiResponseWithWorkflow,
} from "@/api/types";
import {
  WorkflowRunBlock,
  WorkflowRunTimelineBlockItem,
  WorkflowRunTimelineItem,
} from "@/routes/workflows/types/workflowRunTypes";
import {
  buildActionIndex,
  buildBlockStatusMap,
  buildFilmstrip,
  finalizedRunStatus,
  formatRunTimesTooltip,
  runHasOutputs,
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

describe("finalizedRunStatus", () => {
  test("null while there is no status or the run is in-flight", () => {
    expect(finalizedRunStatus(null)).toBeNull();
    expect(finalizedRunStatus(undefined)).toBeNull();
    expect(finalizedRunStatus(Status.Created)).toBeNull();
    expect(finalizedRunStatus(Status.Queued)).toBeNull();
    expect(finalizedRunStatus(Status.Running)).toBeNull();
    expect(finalizedRunStatus(Status.Paused)).toBeNull();
  });

  test("preserves the real terminal status instead of collapsing it", () => {
    expect(finalizedRunStatus(Status.Completed)).toBe(Status.Completed);
    expect(finalizedRunStatus(Status.Failed)).toBe(Status.Failed);
    expect(finalizedRunStatus(Status.Terminated)).toBe(Status.Terminated);
    expect(finalizedRunStatus(Status.TimedOut)).toBe(Status.TimedOut);
    expect(finalizedRunStatus(Status.Canceled)).toBe(Status.Canceled);
  });
});

describe("runHasOutputs", () => {
  type Source = NonNullable<Parameters<typeof runHasOutputs>[0]>;

  function outputsSource(overrides: Partial<Source> = {}): Source {
    return {
      outputs: null,
      errors: null,
      downloaded_files: null,
      downloaded_file_urls: null,
      task_v2: null,
      webhook_failure_reason: null,
      ...overrides,
    };
  }

  function taskV2(overrides: Partial<TaskV2> = {}): TaskV2 {
    return {
      task_id: "task_1",
      status: Status.Completed,
      workflow_run_id: null,
      workflow_id: null,
      workflow_permanent_id: null,
      prompt: null,
      url: null,
      created_at: "2026-01-01T00:00:00Z",
      modified_at: "2026-01-01T00:00:00Z",
      output: null,
      summary: null,
      webhook_callback_url: null,
      webhook_failure_reason: null,
      totp_verification_url: null,
      totp_identifier: null,
      proxy_location: null,
      extra_http_headers: null,
      ...overrides,
    };
  }

  test("false for a missing run", () => {
    expect(runHasOutputs(null)).toBe(false);
    expect(runHasOutputs(undefined)).toBe(false);
  });

  test("false when every signal is empty", () => {
    expect(runHasOutputs(outputsSource())).toBe(false);
  });

  test("true for a record-shaped error", () => {
    expect(
      runHasOutputs(outputsSource({ errors: [{ error_code: "E1" }] })),
    ).toBe(true);
  });

  test("false when the errors array holds no record entries", () => {
    // Mirrors RunView's normalizeRunOutputErrors isRecord filter.
    const nonRecordErrors = ["not a record"] as unknown as Array<
      Record<string, unknown>
    >;
    expect(runHasOutputs(outputsSource({ errors: nonRecordErrors }))).toBe(
      false,
    );
  });

  test("true when extracted_information has a non-null value", () => {
    expect(
      runHasOutputs(
        outputsSource({ outputs: { extracted_information: { a: "b" } } }),
      ),
    ).toBe(true);
  });

  test("false when extracted_information values are all null", () => {
    expect(
      runHasOutputs(
        outputsSource({ outputs: { extracted_information: { a: null } } }),
      ),
    ).toBe(false);
  });

  // extracted_information is cast to Record<string, unknown> without a runtime
  // check (RunView.tsx), so a string/array/etc. flows through Object.values
  // as-is. These pin that behavior against a well-meaning isRecord "cleanup".
  test("true when extracted_information is a plain string", () => {
    expect(
      runHasOutputs(
        outputsSource({ outputs: { extracted_information: "some text" } }),
      ),
    ).toBe(true);
  });

  test("false when extracted_information is explicitly null", () => {
    expect(
      runHasOutputs(
        outputsSource({ outputs: { extracted_information: null } }),
      ),
    ).toBe(false);
  });

  test("false when extracted_information is an empty object", () => {
    expect(
      runHasOutputs(outputsSource({ outputs: { extracted_information: {} } })),
    ).toBe(false);
  });

  test("false when extracted_information is absent from outputs", () => {
    expect(
      runHasOutputs(outputsSource({ outputs: { other_field: "x" } })),
    ).toBe(false);
  });

  test("true for rich downloaded_files", () => {
    expect(
      runHasOutputs(
        outputsSource({
          downloaded_files: [
            {
              url: "https://example.test/a.pdf",
              filename: "a.pdf",
              checksum: null,
              file_size: null,
              modified_at: null,
              artifact_id: null,
            },
          ],
        }),
      ),
    ).toBe(true);
  });

  test("true for downloaded_file_urls with no rich file metadata", () => {
    expect(
      runHasOutputs(
        outputsSource({
          downloaded_file_urls: ["https://example.test/a.pdf"],
        }),
      ),
    ).toBe(true);
  });

  test("true for a task 2.0 observer output", () => {
    expect(
      runHasOutputs(outputsSource({ task_v2: taskV2({ output: { a: 1 } }) })),
    ).toBe(true);
  });

  test("true for a task 2.0 webhook failure reason", () => {
    expect(
      runHasOutputs(
        outputsSource({
          task_v2: taskV2({ webhook_failure_reason: "x" }),
        }),
      ),
    ).toBe(true);
  });

  test("true for a top-level webhook failure reason with no task_v2", () => {
    expect(runHasOutputs(outputsSource({ webhook_failure_reason: "x" }))).toBe(
      true,
    );
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
    expect(frames.map((f) => f.blockType)).toEqual(["task", "task"]);
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

describe("formatRunTimesTooltip", () => {
  function run(
    overrides: Partial<WorkflowRunStatusApiResponseWithWorkflow> = {},
  ): WorkflowRunStatusApiResponseWithWorkflow {
    return {
      status: Status.Completed,
      created_at: "2026-06-30T23:59:00Z",
      queued_at: "2026-06-30T23:59:30Z",
      started_at: "2026-07-01T00:00:00Z",
      finished_at: "2026-07-01T00:05:00Z",
      ...overrides,
    } as WorkflowRunStatusApiResponseWithWorkflow;
  }

  test("lists created, queued, started, finished for a finalized run", () => {
    const title = formatRunTimesTooltip(run());
    expect(title).toContain("Created");
    expect(title).toContain("Queued");
    expect(title).toContain("Started");
    expect(title).toContain("Finished");
    expect(title.split("\n")).toHaveLength(4);
  });

  test("omits absent timestamps and holds Finished until the run finalizes", () => {
    const title = formatRunTimesTooltip(
      run({ status: Status.Running, queued_at: null, started_at: null }),
    );
    expect(title).toContain("Created");
    expect(title).not.toContain("Queued");
    expect(title).not.toContain("Started");
    // finished_at is present but the run is not finalized → still hidden.
    expect(title).not.toContain("Finished");
  });
});
