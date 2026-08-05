import { describe, expect, test } from "vitest";

import {
  ACTIVE_RUN_SESSION_REFETCH_INTERVAL_MS,
  getActiveRunSessionRefetchInterval,
} from "./useActiveRunSessionQuery";

describe("getActiveRunSessionRefetchInterval", () => {
  test("polls while the local Copilot turn is active", () => {
    expect(
      getActiveRunSessionRefetchInterval(
        { data: { active_run_session_id: null } },
        true,
      ),
    ).toBe(ACTIVE_RUN_SESSION_REFETCH_INTERVAL_MS);
  });

  test("keeps polling a server-reported run after a mid-run reload", () => {
    expect(
      getActiveRunSessionRefetchInterval(
        { data: { active_run_session_id: "pbs_run" } },
        false,
      ),
    ).toBe(ACTIVE_RUN_SESSION_REFETCH_INTERVAL_MS);
  });

  test("stops after both local and server activity are terminal", () => {
    expect(
      getActiveRunSessionRefetchInterval(
        { data: { active_run_session_id: null } },
        false,
      ),
    ).toBe(false);
  });
});
