import { AxiosError, AxiosHeaders, CanceledError } from "axios";
import { describe, expect, it } from "vitest";

import { retryTransientNetworkFailures } from "./QueryClient";
import { isTransientNetworkError } from "./transientNetworkError";

function axiosErrorWithStatus(status: number): AxiosError {
  const error = new AxiosError("request failed");
  error.response = {
    status,
    statusText: "",
    data: null,
    headers: {},
    config: { headers: new AxiosHeaders() },
  };
  return error;
}

describe("isTransientNetworkError", () => {
  it("matches axios errors without a response (transport failures)", () => {
    expect(isTransientNetworkError(new AxiosError("Network Error"))).toBe(true);
    expect(
      isTransientNetworkError(
        new AxiosError("Network Error", AxiosError.ERR_NETWORK),
      ),
    ).toBe(true);
  });

  it("excludes cancellations even though they lack a response", () => {
    expect(isTransientNetworkError(new CanceledError())).toBe(false);
    expect(
      isTransientNetworkError(
        new AxiosError("canceled", AxiosError.ERR_CANCELED),
      ),
    ).toBe(false);
  });

  it("excludes HTTP error responses", () => {
    expect(isTransientNetworkError(axiosErrorWithStatus(500))).toBe(false);
    expect(isTransientNetworkError(axiosErrorWithStatus(404))).toBe(false);
  });

  it("excludes non-axios errors", () => {
    expect(isTransientNetworkError(new Error("boom"))).toBe(false);
    expect(isTransientNetworkError("nope")).toBe(false);
    expect(isTransientNetworkError(undefined)).toBe(false);
  });
});

describe("retryTransientNetworkFailures", () => {
  it("retries transport failures up to the cap", () => {
    const networkError = new AxiosError(
      "Network Error",
      AxiosError.ERR_NETWORK,
    );
    expect(retryTransientNetworkFailures(0, networkError)).toBe(true);
    expect(retryTransientNetworkFailures(1, networkError)).toBe(true);
    expect(retryTransientNetworkFailures(2, networkError)).toBe(false);
  });

  it("does not retry HTTP errors or cancellations", () => {
    expect(retryTransientNetworkFailures(0, axiosErrorWithStatus(500))).toBe(
      false,
    );
    expect(retryTransientNetworkFailures(0, new CanceledError())).toBe(false);
  });
});
