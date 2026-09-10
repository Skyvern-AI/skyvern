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
import capturedRun from "./__fixtures__/completed-code-block-run.json";
import {
  actionLabel,
  buildActionIndex,
  buildBlockStatusMap,
  buildFilmstrip,
  finalizedRunStatus,
  formatRunTimesTooltip,
  resolveLandingSelectionId,
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

  // These two shapes pass an "is it there" check but render as nothing, so
  // counting them opens an Outputs pane with no content in it.
  test("false for an error record carrying neither a code nor a message", () => {
    expect(runHasOutputs(outputsSource({ errors: [{}] }))).toBe(false);
    expect(
      runHasOutputs(outputsSource({ errors: [{ error_code: "  " }] })),
    ).toBe(false);
  });

  test("false for a blank webhook failure reason", () => {
    expect(runHasOutputs(outputsSource({ webhook_failure_reason: "" }))).toBe(
      false,
    );
    expect(
      runHasOutputs(
        outputsSource({ task_v2: taskV2({ webhook_failure_reason: "   " }) }),
      ),
    ).toBe(false);
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

  test("true when outputs carry a code block's returned field", () => {
    expect(
      runHasOutputs(
        outputsSource({
          outputs: {
            get_stars_output: {
              star_count: 22600,
              evidence_text: "22.6k stars",
            },
            extracted_information: [],
          },
        }),
      ),
    ).toBe(true);
  });

  test("false when outputs hold only an empty extracted_information array", () => {
    expect(
      runHasOutputs(outputsSource({ outputs: { extracted_information: [] } })),
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

describe("resolveLandingSelectionId", () => {
  // The captured payload of a real completed run whose two code blocks are the
  // whole workflow: the one that finished last emitted no actions at all.
  function capturedTimeline(): WorkflowRunTimelineItem[] {
    return capturedRun.blocks.map((block) => {
      const item = blockItem({
        workflow_run_block_id: block.workflow_run_block_id,
        block_type: block.block_type as WorkflowRunBlock["block_type"],
        label: block.label,
        status: block.status as Status,
        created_at: block.created_at,
        modified_at: block.modified_at,
        actions: block.actions.map((captured) =>
          action({
            action_id: captured.action_id,
            action_type:
              captured.action_type as ActionsApiResponse["action_type"],
            status: captured.status as Status,
            step_id: captured.step_id,
            action_order: captured.action_order,
            screenshot_artifact_id: captured.screenshot_artifact_id,
          }),
        ),
      });
      return {
        ...item,
        created_at: block.created_at,
        modified_at: block.modified_at,
      };
    });
  }

  test("a finished run whose last executed block ran no actions lands on that block", () => {
    const timeline = capturedTimeline();
    const frames = buildFilmstrip(timeline);
    const lastExecuted = capturedRun.blocks[0]!;

    expect(
      frames.some(
        (frame) => frame.blockId === lastExecuted.workflow_run_block_id,
      ),
    ).toBe(false);
    expect(resolveLandingSelectionId(frames, timeline, true)).toBe(
      lastExecuted.workflow_run_block_id,
    );
  });

  test("a finished run whose last executed block has its own actions keeps that block's last frame", () => {
    const timeline = [
      blockItem({
        workflow_run_block_id: "wrb_first",
        modified_at: "2026-01-01T00:00:01Z",
        actions: [action({ action_id: "act_first" })],
      }),
      blockItem({
        workflow_run_block_id: "wrb_last",
        modified_at: "2026-01-01T00:00:02Z",
        actions: [action({ action_id: "act_last" })],
      }),
    ];
    const frames = buildFilmstrip(timeline);

    expect(resolveLandingSelectionId(frames, timeline, true)).toBe("act_last");
  });

  test("a skipped trailing block never becomes the landing target", () => {
    const timeline = [
      blockItem({
        workflow_run_block_id: "wrb_ran",
        modified_at: "2026-01-01T00:00:01Z",
        actions: [action({ action_id: "act_ran" })],
      }),
      blockItem({
        workflow_run_block_id: "wrb_skipped",
        status: Status.Skipped,
        modified_at: "2026-01-01T00:00:02Z",
        actions: [],
      }),
    ];
    const frames = buildFilmstrip(timeline);

    expect(resolveLandingSelectionId(frames, timeline, true)).toBe("act_ran");
  });

  test("an unfinished run follows the live edge", () => {
    const timeline = capturedTimeline();
    const frames = buildFilmstrip(timeline);

    expect(resolveLandingSelectionId(frames, timeline, false)).toBe(
      frames[frames.length - 1]!.id,
    );
  });

  function at(
    created: string,
    block: Partial<WorkflowRunBlock>,
  ): WorkflowRunTimelineItem {
    return { ...blockItem(block), created_at: created };
  }

  test("a trailing skipped block hands the landing to the last block that ran", () => {
    const timeline = [
      at("2026-01-01T00:00:01Z", {
        workflow_run_block_id: "wrb_actions",
        actions: [action({ action_id: "act_early" })],
      }),
      at("2026-01-01T00:00:02Z", {
        workflow_run_block_id: "wrb_code",
        actions: [],
      }),
      at("2026-01-01T00:00:03Z", {
        workflow_run_block_id: "wrb_skipped",
        status: Status.Skipped,
        actions: [],
      }),
    ];
    const frames = buildFilmstrip(timeline);

    expect(resolveLandingSelectionId(frames, timeline, true)).toBe("wrb_code");
  });

  test("a canceled run keeps the last action of the block it interrupted", () => {
    const timeline = [
      at("2026-01-01T00:00:01Z", {
        workflow_run_block_id: "wrb_code",
        actions: [],
      }),
      at("2026-01-01T00:00:02Z", {
        workflow_run_block_id: "wrb_interrupted",
        status: Status.Running,
        actions: [action({ action_id: "act_last" })],
      }),
    ];
    const frames = buildFilmstrip(timeline);

    expect(resolveLandingSelectionId(frames, timeline, true)).toBe("act_last");
  });

  // modified_at is bumped by any later write — a background block-description
  // update lands one on a block the run left long ago — so it cannot decide
  // which block ran last.
  test("a late write on an earlier block does not make it the landing target", () => {
    const timeline = [
      at("2026-01-01T00:00:01Z", {
        workflow_run_block_id: "wrb_early",
        // The background block-description write that lands after the run moved on.
        modified_at: "2026-01-01T00:00:09Z",
        actions: [action({ action_id: "act_early" })],
      }),
      at("2026-01-01T00:00:02Z", {
        workflow_run_block_id: "wrb_code",
        modified_at: "2026-01-01T00:00:03Z",
        actions: [],
      }),
    ];
    const frames = buildFilmstrip(timeline);

    expect(resolveLandingSelectionId(frames, timeline, true)).toBe("wrb_code");
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

describe("actionLabel", () => {
  // Task V3 stamps every action's description with its tool call, so a description-first fallback
  // labelled every v3 frame with machine syntax instead of what the agent said it was doing.
  test("prefers the action's own prose over a Task V3 tool-call stamp", () => {
    expect(
      actionLabel(
        action({
          description: "task_v3 click #sign-in",
          reasoning: "Submitting the sign-in form",
        }),
      ),
    ).toBe("Submitting the sign-in form");
  });

  test("falls back to the readable type when a v3 action carries no prose", () => {
    expect(
      actionLabel(
        action({
          action_type: "click",
          description: "task_v3 click #sign-in",
          reasoning: null,
          intention: null,
        }),
      ),
    ).toBe("Click");
  });

  test("still uses a non-v3 description", () => {
    expect(
      actionLabel(
        action({ description: "Open the billing page", reasoning: null }),
      ),
    ).toBe("Open the billing page");
  });
});
