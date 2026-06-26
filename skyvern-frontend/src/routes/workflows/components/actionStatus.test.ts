import { describe, expect, test } from "vitest";

import {
  ActionTypes,
  Status,
  type ActionsApiResponse,
  type ActionType,
} from "@/api/types";

import {
  getActionDisplayKind,
  getActionDisplayStatus,
  isActionSuccess,
} from "./actionStatus";

function buildAction(
  action_type: ActionType,
  status: Status,
): ActionsApiResponse {
  return {
    action_id: "act_1",
    action_type,
    status,
    task_id: null,
    step_id: null,
    step_order: null,
    action_order: null,
    confidence_float: null,
    description: null,
    reasoning: null,
    intention: null,
    response: null,
    created_by: null,
    text: null,
  };
}

describe("getActionDisplayStatus", () => {
  test("terminate is shown as terminated even when persisted as completed", () => {
    expect(
      getActionDisplayStatus(
        buildAction(ActionTypes.terminate, Status.Completed),
      ),
    ).toBe(Status.Terminated);
  });

  test("wait passes its persisted status through (timeline shows real status)", () => {
    expect(
      getActionDisplayStatus(buildAction(ActionTypes.wait, Status.Failed)),
    ).toBe(Status.Failed);
  });

  test("other actions pass their persisted status through", () => {
    expect(
      getActionDisplayStatus(buildAction(ActionTypes.Click, Status.Failed)),
    ).toBe(Status.Failed);
    expect(
      getActionDisplayStatus(buildAction(ActionTypes.Click, Status.Completed)),
    ).toBe(Status.Completed);
  });
});

describe("isActionSuccess", () => {
  test("terminate is a failure", () => {
    expect(
      isActionSuccess(buildAction(ActionTypes.terminate, Status.Completed)),
    ).toBe(false);
  });

  test("wait is a success", () => {
    expect(isActionSuccess(buildAction(ActionTypes.wait, Status.Failed))).toBe(
      true,
    );
  });

  test("completed and skipped are successes, failed is not", () => {
    expect(
      isActionSuccess(buildAction(ActionTypes.Click, Status.Completed)),
    ).toBe(true);
    expect(
      isActionSuccess(buildAction(ActionTypes.Click, Status.Skipped)),
    ).toBe(true);
    expect(isActionSuccess(buildAction(ActionTypes.Click, Status.Failed))).toBe(
      false,
    );
  });
});

describe("getActionDisplayKind", () => {
  test("terminate is its own kind, not failure", () => {
    expect(
      getActionDisplayKind(
        buildAction(ActionTypes.terminate, Status.Completed),
      ),
    ).toBe("terminated");
  });

  test("wait is a success", () => {
    expect(
      getActionDisplayKind(buildAction(ActionTypes.wait, Status.Failed)),
    ).toBe("success");
  });

  test("completed is success, failed is failure", () => {
    expect(
      getActionDisplayKind(buildAction(ActionTypes.Click, Status.Completed)),
    ).toBe("success");
    expect(
      getActionDisplayKind(buildAction(ActionTypes.Click, Status.Failed)),
    ).toBe("failure");
  });
});
