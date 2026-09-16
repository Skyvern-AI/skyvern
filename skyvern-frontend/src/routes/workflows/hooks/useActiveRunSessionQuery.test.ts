import { AxiosError, AxiosHeaders } from "axios";
import { describe, expect, test } from "vitest";

import {
  ACTIVE_RUN_SESSION_REFETCH_INTERVAL_MS,
  getActiveRunSessionRefetchInterval,
} from "./useActiveRunSessionQuery";

function forbiddenError(status = 403): AxiosError {
  const error = new AxiosError(`Request failed with status code ${status}`);
  error.response = {
    status,
    statusText: "",
    data: null,
    headers: {},
    config: { headers: new AxiosHeaders() },
  };
  return error;
}

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

  test("stops polling on a forbidden viewer-state even while active", () => {
    expect(
      getActiveRunSessionRefetchInterval(
        {
          status: "error",
          data: { active_run_session_id: "pbs_run" },
          error: forbiddenError(403),
        },
        true,
      ),
    ).toBe(false);
    expect(
      getActiveRunSessionRefetchInterval(
        { status: "error", error: forbiddenError(401) },
        false,
      ),
    ).toBe(false);
  });
});
