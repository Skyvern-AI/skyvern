import { describe, expect, test } from "vitest";

import { Status } from "@/api/types";

import {
  getRunStatusRefetchInterval,
  RUN_STATUS_POLL_INTERVAL_MS,
} from "./useWorkflowRunQuery";

describe("getRunStatusRefetchInterval", () => {
  test("stops polling once the query is in an error state, even with stale non-finalized data", () => {
    expect(
      getRunStatusRefetchInterval({
        status: "error",
        data: { status: Status.Running },
      }),
    ).toBe(false);
  });

  test("does not poll before the first successful fetch", () => {
    expect(
      getRunStatusRefetchInterval({ status: "pending", data: undefined }),
    ).toBe(false);
  });

  test("polls while the run is not finalized", () => {
    expect(
      getRunStatusRefetchInterval({
        status: "success",
        data: { status: Status.Running },
      }),
    ).toBe(RUN_STATUS_POLL_INTERVAL_MS);
  });

  test("stops polling once the run is finalized", () => {
    expect(
      getRunStatusRefetchInterval({
        status: "success",
        data: { status: Status.Completed },
      }),
    ).toBe(false);
  });
});
