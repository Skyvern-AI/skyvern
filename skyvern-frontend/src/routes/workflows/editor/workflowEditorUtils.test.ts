import { describe, expect, test } from "vitest";

import type { AppNode } from "./nodes";
import {
  blockRunErrors,
  getWorkflowErrors,
  pendingGoalChangesOf,
  withGoalUndoRecordsFrom,
} from "./workflowEditorUtils";

function codeBlock(goalNeedsRegeneration: boolean | null): AppNode {
  return {
    id: "cb1",
    type: "codeBlock",
    position: { x: 0, y: 0 },
    data: {
      debuggable: true,
      editable: true,
      label: "lookup_invoice",
      code: "print(1)",
      continueOnFailure: false,
      parameterKeys: [],
      errorCodeMapping: "null",
      prompt: "Download last month's invoice",
      steps: null,
      dataSchema: "null",
      userOwnedGoal: true,
      goalNeedsRegeneration,
      model: null,
    },
  } as AppNode;
}

describe("getWorkflowErrors", () => {
  test("refuses to save a code block whose edited Goal has not been rebuilt into code", () => {
    const errors = getWorkflowErrors([codeBlock(true)]);

    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("lookup_invoice");
  });

  test("a rebuild flag on a Goal no person owns blocks nothing, as on the backend", () => {
    const apiAuthored = {
      ...codeBlock(true),
      data: { ...codeBlock(true).data, userOwnedGoal: null },
    } as AppNode;

    expect(getWorkflowErrors([apiAuthored])).toEqual([]);
  });

  test("saves once the code has been rebuilt from the Goal", () => {
    expect(getWorkflowErrors([codeBlock(false)])).toEqual([]);
    expect(getWorkflowErrors([codeBlock(null)])).toEqual([]);
  });
});

describe("withGoalUndoRecordsFrom", () => {
  const edited = {
    ...codeBlock(true),
    data: {
      ...codeBlock(true).data,
      goalBeforeEdit: {
        prompt: "Download the latest statement",
        userOwnedGoal: null,
        goalNeedsRegeneration: null,
      },
    },
  } as AppNode;

  test("a draft that keeps the unapplied Goal keeps its Undo", () => {
    const rebuilt = withGoalUndoRecordsFrom([edited], [codeBlock(true)]);

    expect(pendingGoalChangesOf(rebuilt)).toEqual([
      {
        label: "lookup_invoice",
        goal: "Download last month's invoice",
        previousGoal: "Download the latest statement",
      },
    ]);
  });

  test("a draft that applied the Goal, or changed its text, drops the Undo", () => {
    const applied = withGoalUndoRecordsFrom([edited], [codeBlock(false)]);
    const retyped = withGoalUndoRecordsFrom(
      [edited],
      [
        {
          ...codeBlock(true),
          data: { ...codeBlock(true).data, prompt: "Something else" },
        } as AppNode,
      ],
    );

    expect(pendingGoalChangesOf(applied)).toEqual([]);
    expect(pendingGoalChangesOf(retyped)[0]?.previousGoal).toBeNull();
  });
});

describe("blockRunErrors", () => {
  const otherBlock = {
    ...codeBlock(null),
    id: "cb2",
    data: { ...codeBlock(null).data, label: "send_report" },
  } as AppNode;

  test("a pending Goal on another block stops a single-block run", () => {
    const errors = blockRunErrors([codeBlock(true), otherBlock], "send_report");

    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("lookup_invoice");
  });

  test("with no pending Goal, only the run block's own errors stop it", () => {
    expect(
      blockRunErrors([codeBlock(false), otherBlock], "send_report"),
    ).toEqual([]);
  });
});
