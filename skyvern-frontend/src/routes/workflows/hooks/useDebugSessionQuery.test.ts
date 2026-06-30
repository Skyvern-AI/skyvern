import { describe, expect, test } from "vitest";

import {
  DEBUG_SESSION_ERROR_REFETCH_INTERVAL_MS,
  DEBUG_SESSION_KEEP_ALIVE_INTERVAL_MS,
  getDebugSessionRefetchInterval,
} from "./useDebugSessionQuery";

describe("getDebugSessionRefetchInterval", () => {
  test("keeps debug sessions alive after a browser session exists when enabled", () => {
    expect(
      getDebugSessionRefetchInterval(
        {
          status: "success",
          data: { browser_session_id: "pbs_123" },
        },
        false,
        true,
      ),
    ).toBe(DEBUG_SESSION_KEEP_ALIVE_INTERVAL_MS);
  });

  test("does not keep successful debug-session reads alive by default", () => {
    expect(
      getDebugSessionRefetchInterval({
        status: "success",
        data: { browser_session_id: "pbs_123" },
      }),
    ).toBe(false);
  });

  test("does not poll successful responses that have no browser session yet", () => {
    expect(
      getDebugSessionRefetchInterval(
        {
          status: "success",
          data: { browser_session_id: "" },
        },
        false,
        true,
      ),
    ).toBe(false);
    expect(
      getDebugSessionRefetchInterval(
        {
          status: "success",
          data: { browser_session_id: null },
        },
        false,
        true,
      ),
    ).toBe(false);
  });

  test("uses slower polling for errors", () => {
    expect(getDebugSessionRefetchInterval({ status: "error" })).toBe(
      DEBUG_SESSION_ERROR_REFETCH_INTERVAL_MS,
    );
  });

  test("does not poll while rate limited", () => {
    expect(
      getDebugSessionRefetchInterval(
        {
          status: "success",
          data: { browser_session_id: "pbs_123" },
        },
        true,
        true,
      ),
    ).toBe(false);
  });
});
