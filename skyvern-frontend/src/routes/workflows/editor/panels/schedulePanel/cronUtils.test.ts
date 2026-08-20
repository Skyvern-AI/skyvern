import { describe, expect, it } from "vitest";
import { meetsMinCronInterval } from "./cronUtils";

describe("meetsMinCronInterval", () => {
  it("accepts a cron that fires exactly at the minimum interval", () => {
    expect(meetsMinCronInterval("*/5 * * * *")).toBe(true);
  });

  it("rejects a cron whose tight gap is hidden outside a short sample window", () => {
    // Fires every 5 min except a cluster at :55/:59 (4 min apart) and the
    // :59 -> :00-next-hour wrap (1 min apart) -- both violate the 5-minute
    // floor, but only show up if the scan covers a full cycle, not just the
    // first couple of runs.
    expect(
      meetsMinCronInterval("0,5,10,15,20,25,30,35,40,45,50,55,59 * * * *"),
    ).toBe(false);
  });

  it("rejects a cron that fires more often than the minimum everywhere", () => {
    expect(meetsMinCronInterval("*/1 * * * *")).toBe(false);
  });

  it("returns false for an invalid cron expression instead of throwing", () => {
    expect(meetsMinCronInterval("not a cron")).toBe(false);
  });

  it("honors a custom minimum interval override", () => {
    expect(meetsMinCronInterval("*/10 * * * *", 5 * 60)).toBe(true);
    expect(meetsMinCronInterval("*/10 * * * *", 15 * 60)).toBe(false);
  });
});
