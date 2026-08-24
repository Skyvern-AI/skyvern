import { describe, expect, test } from "vitest";

import { runViewTabBasePath } from "./runViewTabBasePath";

describe("runViewTabBasePath keeps each mounted route family", () => {
  test("agent long-form route -> /agents/{workflowPermanentId}/{workflowRunId}", () => {
    expect(
      runViewTabBasePath({
        workflowPermanentId: "wpid_1",
        workflowRunId: "wr_1",
      }),
    ).toBe("/agents/wpid_1/wr_1");
  });

  test("task route -> /tasks/{taskId}", () => {
    expect(runViewTabBasePath({ taskId: "tsk_1" })).toBe("/tasks/tsk_1");
  });

  test("runs splat -> /runs/{runId}", () => {
    expect(runViewTabBasePath({ runId: "wr_1" })).toBe("/runs/wr_1");
  });
});
