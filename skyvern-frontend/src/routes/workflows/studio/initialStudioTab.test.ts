import { describe, expect, test } from "vitest";

import { initialStudioTab } from "./constants";

describe("initialStudioTab", () => {
  test("opens the Run tab for a run deep link (block or full run)", () => {
    expect(initialStudioTab({ runId: "wr_123", active: null })).toBe("run");
  });

  test("opens the Run tab when only a pinned item is deep linked", () => {
    expect(initialStudioTab({ runId: null, active: "act_1" })).toBe("run");
  });

  test("opens the Run tab when both a run and a pinned item are present", () => {
    expect(initialStudioTab({ runId: "wr_123", active: "act_1" })).toBe("run");
  });

  test("opens the Editor with no run reference", () => {
    expect(initialStudioTab({ runId: null, active: null })).toBe("editor");
  });
});
