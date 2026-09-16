import { AxiosError, AxiosHeaders } from "axios";
import { describe, expect, test } from "vitest";

import {
  DEBUG_SESSION_ERROR_REFETCH_INTERVAL_MS,
  DEBUG_SESSION_KEEP_ALIVE_INTERVAL_MS,
  DEBUG_SESSION_MAX_RETRIES,
  getDebugSessionRefetchInterval,
  shouldPollDebugSessionInvalidation,
  shouldRetryDebugSessionRead,
} from "./useDebugSessionQuery";

function axiosErrorWithStatus(status: number): AxiosError {
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

function paymentRequiredError(): AxiosError {
  return axiosErrorWithStatus(402);
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

  test("stops polling on a forbidden or expired session (401/403)", () => {
    expect(
      getDebugSessionRefetchInterval(
        { status: "error", error: axiosErrorWithStatus(403) },
        false,
        true,
      ),
    ).toBe(false);
    expect(
      getDebugSessionRefetchInterval(
        { status: "error", error: axiosErrorWithStatus(401) },
        false,
        true,
      ),
    ).toBe(false);
  });
});

describe("shouldPollDebugSessionInvalidation", () => {
  const baseState = {
    debugSession: null,
    debugSessionError: undefined as unknown,
    shouldFetchDebugSession: true,
    workflowPermanentId: "wpid_123",
    isRateLimited: false,
  };

  test("polls while waiting for a browser session with no error", () => {
    expect(shouldPollDebugSessionInvalidation(baseState)).toBe(true);
  });

  test("keeps polling through a non-terminal error", () => {
    expect(
      shouldPollDebugSessionInvalidation({
        ...baseState,
        debugSessionError: new AxiosError("Network Error"),
      }),
    ).toBe(true);
  });

  test("stops polling on a forbidden or expired session (401/403)", () => {
    expect(
      shouldPollDebugSessionInvalidation({
        ...baseState,
        debugSessionError: axiosErrorWithStatus(403),
      }),
    ).toBe(false);
    expect(
      shouldPollDebugSessionInvalidation({
        ...baseState,
        debugSessionError: axiosErrorWithStatus(401),
      }),
    ).toBe(false);
  });

  test("stops polling when the org is out of credits (402)", () => {
    expect(
      shouldPollDebugSessionInvalidation({
        ...baseState,
        debugSessionError: paymentRequiredError(),
      }),
    ).toBe(false);
  });

  test("stops once a browser session exists", () => {
    expect(
      shouldPollDebugSessionInvalidation({
        ...baseState,
        debugSession: { browser_session_id: "pbs_123" },
      }),
    ).toBe(false);
  });

  test("does not poll while rate limited or when fetching is disabled", () => {
    expect(
      shouldPollDebugSessionInvalidation({ ...baseState, isRateLimited: true }),
    ).toBe(false);
    expect(
      shouldPollDebugSessionInvalidation({
        ...baseState,
        shouldFetchDebugSession: false,
      }),
    ).toBe(false);
    expect(
      shouldPollDebugSessionInvalidation({
        ...baseState,
        workflowPermanentId: undefined,
      }),
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

  test("does not retry a forbidden or expired session (401/403)", () => {
    expect(shouldRetryDebugSessionRead(0, axiosErrorWithStatus(403))).toBe(
      false,
    );
    expect(shouldRetryDebugSessionRead(0, axiosErrorWithStatus(401))).toBe(
      false,
    );
  });
});
