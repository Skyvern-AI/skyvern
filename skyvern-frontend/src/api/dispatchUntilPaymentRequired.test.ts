import { AxiosError, AxiosHeaders } from "axios";
import { describe, expect, it } from "vitest";

import { dispatchUntilPaymentRequired } from "./dispatchUntilPaymentRequired";
import { isPaymentRequiredError } from "./paymentRequired";

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

describe("isPaymentRequiredError", () => {
  it("matches only 402 axios errors", () => {
    expect(isPaymentRequiredError(axiosErrorWithStatus(402))).toBe(true);
    expect(isPaymentRequiredError(axiosErrorWithStatus(403))).toBe(false);
    expect(isPaymentRequiredError(new AxiosError("Network Error"))).toBe(false);
    expect(isPaymentRequiredError(new Error("boom"))).toBe(false);
    expect(isPaymentRequiredError(undefined)).toBe(false);
  });
});

describe("dispatchUntilPaymentRequired", () => {
  it("stops dispatching after the first 402", async () => {
    const dispatched: Array<string> = [];
    const items = Array.from({ length: 50 }, (_, index) => `url-${index}`);

    const outcome = await dispatchUntilPaymentRequired(
      items,
      async (url) => {
        dispatched.push(url);
        throw axiosErrorWithStatus(402);
      },
      2,
    );

    expect(outcome.paymentRequired).toBe(true);
    // The pool has two workers, so at most one extra request is already in
    // flight when the first 402 lands.
    expect(dispatched.length).toBeLessThanOrEqual(2);
    expect(outcome.dispatchedCount).toBe(dispatched.length);
    expect(outcome.skippedCount).toBe(items.length - dispatched.length);
  });

  it("dispatches every item when nothing is payment required", async () => {
    const items = ["a", "b", "c", "d", "e"];

    const outcome = await dispatchUntilPaymentRequired(
      items,
      async (url) => url.toUpperCase(),
      2,
    );

    expect(outcome.paymentRequired).toBe(false);
    expect(outcome.dispatchedCount).toBe(items.length);
    expect(outcome.skippedCount).toBe(0);
    expect(outcome.results).toEqual(
      items.map((url) => ({ status: "fulfilled", value: url.toUpperCase() })),
    );
  });

  it("keeps going past non-402 failures", async () => {
    const items = ["a", "b", "c"];

    const outcome = await dispatchUntilPaymentRequired(
      items,
      async (url) => {
        if (url === "a") {
          throw axiosErrorWithStatus(500);
        }
        return url;
      },
      1,
    );

    expect(outcome.paymentRequired).toBe(false);
    expect(outcome.dispatchedCount).toBe(3);
    expect(outcome.results[0]?.status).toBe("rejected");
    expect(outcome.results[1]).toEqual({ status: "fulfilled", value: "b" });
    expect(outcome.results[2]).toEqual({ status: "fulfilled", value: "c" });
  });

  it("settles failures instead of rejecting", async () => {
    const outcome = await dispatchUntilPaymentRequired(
      ["a", "b", "c"],
      async () => {
        throw axiosErrorWithStatus(402);
      },
      3,
    );

    expect(outcome.paymentRequired).toBe(true);
    expect(
      outcome.results.filter((result) => result?.status === "rejected"),
    ).toHaveLength(3);
  });

  it("handles an empty item list", async () => {
    const outcome = await dispatchUntilPaymentRequired([], async () => "never");

    expect(outcome.paymentRequired).toBe(false);
    expect(outcome.dispatchedCount).toBe(0);
    expect(outcome.skippedCount).toBe(0);
    expect(outcome.results).toEqual([]);
  });
});
