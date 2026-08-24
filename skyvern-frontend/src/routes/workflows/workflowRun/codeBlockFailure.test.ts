import { describe, expect, test } from "vitest";

import { ActionTypes, Status, type ActionsApiResponse } from "@/api/types";

import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import {
  describeCodeBlockFailure,
  failingCodeLineFromActions,
  findRunCodeBlockFailure,
} from "./codeBlockFailure";

type FailedBlock = Pick<
  WorkflowRunBlock,
  "block_type" | "failure_reason" | "error_codes" | "actions" | "output"
>;

function block(overrides: Partial<FailedBlock> = {}): FailedBlock {
  return {
    block_type: "code",
    failure_reason: null,
    error_codes: null,
    actions: null,
    output: null,
    ...overrides,
  };
}

function failedAction(codeLine: number | null): ActionsApiResponse {
  return {
    action_id: `act_${codeLine ?? "none"}`,
    action_type: ActionTypes.NullAction,
    status: Status.Failed,
    task_id: null,
    step_id: null,
    step_order: null,
    action_order: 0,
    confidence_float: null,
    description: null,
    reasoning: null,
    intention: null,
    response: null,
    created_by: null,
    text: null,
    output: codeLine === null ? null : { code_line: codeLine },
  };
}

function timelineBlock(
  overrides: Partial<WorkflowRunBlock>,
): WorkflowRunTimelineItem {
  const failureBlock = {
    ...block(overrides),
    workflow_run_block_id: overrides.workflow_run_block_id ?? "wrb_code",
    continue_on_failure: overrides.continue_on_failure ?? false,
  } as WorkflowRunBlock;
  return {
    type: "block",
    block: failureBlock,
    children: [],
    thought: null,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
  };
}

describe("describeCodeBlockFailure", () => {
  test("returns null for blocks that are not code blocks", () => {
    expect(
      describeCodeBlockFailure(
        block({ block_type: "task", failure_reason: "task block failed" }),
      ),
    ).toBeNull();
  });

  test("returns null when there is nothing to classify", () => {
    expect(describeCodeBlockFailure(block())).toBeNull();
  });

  test("treats a sandbox fault as a retry, never a code fix", () => {
    const failure = describeCodeBlockFailure(
      block({
        error_codes: ["runner_unavailable"],
        failure_reason: "Secure CodeBlock runner is unavailable. Please retry.",
      }),
    );
    expect(failure).toMatchObject({
      kind: "infrastructure",
      code: "runner_unavailable",
      recovery: "retry",
    });
  });

  test("a busy runner is an infrastructure state, not a code defect", () => {
    expect(
      describeCodeBlockFailure(block({ error_codes: ["busy"] })),
    ).toMatchObject({ kind: "infrastructure", recovery: "retry" });
  });

  test("names the exception and line for a user code error", () => {
    const failure = describeCodeBlockFailure(
      block({
        error_codes: ["user_code_error"],
        failure_reason:
          "CodeBlock failed with NameError at line 6: name 'min' is not defined.",
      }),
    );
    expect(failure).toMatchObject({
      kind: "user-code",
      title: "The block's code raised NameError",
      line: 6,
      recovery: "fix",
    });
  });

  test("a page failure reads as a browser problem, not a code defect", () => {
    expect(
      describeCodeBlockFailure(
        block({
          error_codes: ["browser_operation_failed"],
          failure_reason:
            "CodeBlock failed because a browser operation failed at line 12: net::ERR_CONNECTION_CLOSED",
        }),
      ),
    ).toMatchObject({
      kind: "browser",
      code: "browser_operation_failed",
      line: 12,
      recovery: "either",
    });
  });

  test("prefers the workflow's declared code over the transport code", () => {
    const failure = describeCodeBlockFailure(
      block({
        error_codes: ["user_code_error", "no_tables_available"],
        failure_reason: "No tables were available at the requested time.",
      }),
    );
    expect(failure).toMatchObject({
      kind: "declared",
      code: "no_tables_available",
      title: 'The workflow reported "no_tables_available"',
      recovery: "retry",
    });
  });

  test("does not treat an unmapped runner code as workflow-declared", () => {
    const failure = describeCodeBlockFailure(
      block({
        error_codes: ["future_runner_error"],
        failure_reason: "The code block runner reported a new failure.",
        output: {
          errors: [{ error_code: "future_runner_error" }],
        },
      }),
    );
    expect(failure).toMatchObject({
      kind: "user-code",
      code: "future_runner_error",
      title: "The code block failed",
      recovery: "either",
    });
  });

  test("does not treat two unknown runner codes as workflow-declared", () => {
    const failure = describeCodeBlockFailure(
      block({
        error_codes: ["future_runner_error", "another_runner_error"],
        failure_reason: "The code block runner reported a new failure.",
      }),
    );
    expect(failure).toMatchObject({
      kind: "user-code",
      code: "future_runner_error",
      title: "The code block failed",
      recovery: "either",
    });
  });

  test("uses the backend error type for an inline declared code", () => {
    const failure = describeCodeBlockFailure(
      block({
        error_codes: ["no_tables_available"],
        failure_reason: "No tables were available at the requested time.",
        output: {
          errors: [
            {
              error_code: "no_tables_available",
              error_type: "USER_DEFINED_ERROR",
            },
          ],
        },
      }),
    );
    expect(failure).toMatchObject({
      kind: "declared",
      code: "no_tables_available",
      title: 'The workflow reported "no_tables_available"',
      recovery: "retry",
    });
  });

  test("keeps code-specific guidance when the reason names an exception", () => {
    const failure = describeCodeBlockFailure(
      block({
        error_codes: ["insecure_code_detected"],
        failure_reason:
          "CodeBlock failed with InsecureCodeError at line 4: blocked operation.",
      }),
    );
    expect(failure).toMatchObject({
      title: "The block's code raised InsecureCodeError",
      guidance:
        "Code blocks run in a locked-down sandbox. Rewrite the step using a supported Skyvern helper instead.",
    });
  });

  test("classifies the inline engine's prose when no error code is persisted", () => {
    expect(
      describeCodeBlockFailure(
        block({
          failure_reason:
            "Failed to execute code block. Reason: TimeoutError: code block exceeded 300 seconds",
        }),
      ),
    ).toMatchObject({ kind: "limit", code: "timeout" });
  });

  test("keeps a named user exception ahead of infrastructure wording", () => {
    expect(
      describeCodeBlockFailure(
        block({
          failure_reason:
            "Failed to execute code block. Reason: RuntimeError: browser disconnected while polling",
        }),
      ),
    ).toMatchObject({
      kind: "user-code",
      title: "The block's code raised RuntimeError",
      recovery: "fix",
    });
  });

  test("calls out an ErrorCode that is missing from the mapping", () => {
    const failure = describeCodeBlockFailure(
      block({
        failure_reason:
          "Failed to execute code block. Reason: ErrorCode is not declared in the effective error_code_mapping",
      }),
    );
    expect(failure).toMatchObject({
      kind: "user-code",
      title: "The block raised an undeclared error code",
      recovery: "fix",
    });
    expect(failure?.guidance).toContain("error code mapping");
  });

  test("names the exception behind a redacted inline failure reason", () => {
    expect(
      describeCodeBlockFailure(
        block({
          failure_reason:
            "Failed to execute code block. Reason: SyntaxError: invalid syntax",
        }),
      ),
    ).toMatchObject({
      kind: "user-code",
      title: "The block's code raised SyntaxError",
    });
  });

  test("falls back to the failing action's line when the reason has none", () => {
    expect(
      describeCodeBlockFailure(
        block({
          failure_reason: "Failed to execute code block.",
          actions: [failedAction(null), failedAction(9)],
        }),
      )?.line,
    ).toBe(9);
  });
});

describe("failingCodeLineFromActions", () => {
  test("takes the first failed action carrying a line", () => {
    expect(failingCodeLineFromActions([failedAction(4), failedAction(7)])).toBe(
      4,
    );
  });

  test("returns null without actions", () => {
    expect(failingCodeLineFromActions(null)).toBeNull();
  });
});

describe("findRunCodeBlockFailure", () => {
  test("skips continued blocks when multiple blocks share the run reason", () => {
    const reason = "Failed to execute code block.";
    const timeline = [
      timelineBlock({
        workflow_run_block_id: "wrb_continued",
        failure_reason: reason,
        error_codes: ["runner_unavailable"],
        continue_on_failure: true,
      }),
      timelineBlock({
        workflow_run_block_id: "wrb_culprit",
        failure_reason: reason,
        error_codes: ["user_code_error"],
      }),
    ];

    expect(findRunCodeBlockFailure(reason, timeline)).toMatchObject({
      code: "user_code_error",
      recovery: "fix",
    });
  });

  test("prefers a body failure over a later finally failure", () => {
    const reason = "Failed to execute code block.";
    const timeline = [
      timelineBlock({
        workflow_run_block_id: "wrb_finally",
        label: "cleanup",
        failure_reason: reason,
        error_codes: ["runner_unavailable"],
      }),
      timelineBlock({
        workflow_run_block_id: "wrb_culprit",
        label: "body",
        failure_reason: reason,
        error_codes: ["user_code_error"],
      }),
    ];

    expect(findRunCodeBlockFailure(reason, timeline, "cleanup")).toMatchObject({
      code: "user_code_error",
      recovery: "fix",
    });
  });

  test("uses the finally failure when it is the only matching culprit", () => {
    const reason = "Code block runner is unavailable.";
    const timeline = [
      timelineBlock({
        workflow_run_block_id: "wrb_finally",
        label: "cleanup",
        failure_reason: reason,
        error_codes: ["runner_unavailable"],
      }),
    ];

    expect(findRunCodeBlockFailure(reason, timeline, "cleanup")).toMatchObject({
      code: "runner_unavailable",
      recovery: "retry",
    });
  });
});
