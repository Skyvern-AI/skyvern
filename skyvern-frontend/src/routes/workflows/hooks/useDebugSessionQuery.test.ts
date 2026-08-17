import { AxiosError, AxiosHeaders } from "axios";
import { describe, expect, test } from "vitest";

import {
  DEBUG_SESSION_ERROR_REFETCH_INTERVAL_MS,
  DEBUG_SESSION_KEEP_ALIVE_INTERVAL_MS,
  DEBUG_SESSION_MAX_RETRIES,
  getDebugSessionRefetchInterval,
  shouldRetryDebugSessionRead,
} from "./useDebugSessionQuery";

function paymentRequiredError(): AxiosError {
  const error = new AxiosError("Request failed with status code 402");
  error.response = {
    status: 402,
    statusText: "Payment Required",
    data: null,
    headers: {},
    config: { headers: new AxiosHeaders() },
  };
  return error;
}

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

  test("stops polling when the org is out of credits", () => {
    expect(
      getDebugSessionRefetchInterval(
        { status: "error", error: paymentRequiredError() },
        false,
        true,
      ),
    ).toBe(false);
  });
});

describe("shouldRetryDebugSessionRead", () => {
  test("retries other failures up to the cap", () => {
    const error = new AxiosError("Network Error");
    expect(shouldRetryDebugSessionRead(0, error)).toBe(true);
    expect(
      shouldRetryDebugSessionRead(DEBUG_SESSION_MAX_RETRIES - 1, error),
    ).toBe(true);
    expect(shouldRetryDebugSessionRead(DEBUG_SESSION_MAX_RETRIES, error)).toBe(
      false,
    );
  });

  test("does not retry when the org is out of credits", () => {
    expect(shouldRetryDebugSessionRead(0, paymentRequiredError())).toBe(false);
  });
});
