import { describe, expect, it } from "vitest";

import { runWithConcurrency } from "./runWithConcurrency";

describe("runWithConcurrency", () => {
  it("runs all tasks and returns settled results", async () => {
    const results = await runWithConcurrency(
      [
        () => Promise.resolve("a"),
        () => Promise.reject(new Error("fail")),
        () => Promise.resolve("c"),
      ],
      2,
    );

    expect(results).toHaveLength(3);
    expect(results[0]).toEqual({ status: "fulfilled", value: "a" });
    expect(results[1]?.status).toBe("rejected");
    expect(results[2]).toEqual({ status: "fulfilled", value: "c" });
  });

  it("respects concurrency limit", async () => {
    let inFlight = 0;
    let maxInFlight = 0;

    await runWithConcurrency(
      Array.from({ length: 6 }, () => async () => {
        inFlight += 1;
        maxInFlight = Math.max(maxInFlight, inFlight);
        await new Promise((resolve) => setTimeout(resolve, 5));
        inFlight -= 1;
        return true;
      }),
      2,
    );

    expect(maxInFlight).toBeLessThanOrEqual(2);
  });
});
