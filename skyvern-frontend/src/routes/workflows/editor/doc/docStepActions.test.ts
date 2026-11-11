import { describe, expect, test } from "vitest";
import { Status, type ActionsApiResponse } from "@/api/types";
import { docStepActions, MAX_SHOWN_ACTIONS } from "./docStepActions";

function action(
  id: string,
  extra: Partial<ActionsApiResponse> = {},
): ActionsApiResponse {
  return {
    action_id: id,
    action_type: "click",
    status: Status.Completed,
    intention: `Step ${id}`,
    ...extra,
  } as ActionsApiResponse;
}

describe("docStepActions", () => {
  test("reads oldest first, labels a null action and marks a failed one", () => {
    const { shown, more } = docStepActions([
      action("3", { status: Status.Failed }),
      action("2", { action_type: "null_action" as never, intention: null }),
      action("1"),
    ]);
    expect(shown.map((a) => [a.type, a.failed])).toEqual([
      ["Click", false],
      ["Look at the page", false],
      ["Click", true],
    ]);
    expect(shown[0]!.text).toBe("Step 1");
    expect(more).toBe(0);
  });

  test("shows the first ten that ran and counts the rest", () => {
    const total = MAX_SHOWN_ACTIONS + 3;
    const actions = Array.from({ length: total }, (_, i) =>
      action(`a${i}`, { intention: `Step ${total - i}` }),
    );
    const { shown, more } = docStepActions(actions);
    expect(shown).toHaveLength(MAX_SHOWN_ACTIONS);
    expect(more).toBe(3);
    expect(shown[0]!.text).toBe("Step 1");
  });

  test("does not mark a finished wait as failed", () => {
    const { shown } = docStepActions([
      action("2", { action_type: "wait" as never, status: Status.Failed }),
      action("1", { status: Status.Failed }),
    ]);
    expect(shown.map((a) => [a.type, a.failed])).toEqual([
      ["Click", true],
      ["Wait", false],
    ]);
  });
});
