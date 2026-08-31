import { describe, expect, test } from "vitest";

import {
  runOverviewScreenshotLocation,
  runViewTabBasePath,
} from "./runViewTabBasePath";

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

test("screenshot handoff returns legacy child routes to Overview", () => {
  expect(
    runOverviewScreenshotLocation(
      "/runs/wr_1",
      "?embed=true&active=act_1&iteration=2",
      "wrb_failed",
    ),
  ).toEqual({
    pathname: "/runs/wr_1/overview",
    search: "?embed=true&active=wrb_failed",
  });
});
