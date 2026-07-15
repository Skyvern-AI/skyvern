import { describe, expect, it } from "vitest";
import {
  cronToScheduleBuilder,
  scheduleBuilderToCron,
  scheduleBuildersEquivalent,
  to12Hour,
  to24Hour,
  type ScheduleBuilderState,
} from "./scheduleBuilder";

describe("scheduleBuilderToCron", () => {
  it("builds an hourly expression from the minute only", () => {
    expect(
      scheduleBuilderToCron({
        frequency: "hourly",
        minute: 15,
        hour: 9,
        daysOfWeek: [1],
        dayOfMonth: 1,
      }),
    ).toBe("15 * * * *");
  });

  it("builds a daily expression", () => {
    expect(
      scheduleBuilderToCron({
        frequency: "daily",
        minute: 0,
        hour: 9,
        daysOfWeek: [1],
        dayOfMonth: 1,
      }),
    ).toBe("0 9 * * *");
  });

  it("builds a weekly expression with a sorted, de-duplicated day list", () => {
    expect(
      scheduleBuilderToCron({
        frequency: "weekly",
        minute: 30,
        hour: 8,
        daysOfWeek: [5, 1, 3, 1],
        dayOfMonth: 1,
      }),
    ).toBe("30 8 * * 1,3,5");
  });

  it("builds a monthly expression", () => {
    expect(
      scheduleBuilderToCron({
        frequency: "monthly",
        minute: 0,
        hour: 9,
        daysOfWeek: [1],
        dayOfMonth: 15,
      }),
    ).toBe("0 9 15 * *");
  });
});

describe("cronToScheduleBuilder", () => {
  it("parses the hourly preset", () => {
    expect(cronToScheduleBuilder("0 * * * *")).toMatchObject({
      frequency: "hourly",
      minute: 0,
    });
  });

  it("parses the daily preset", () => {
    expect(cronToScheduleBuilder("0 9 * * *")).toMatchObject({
      frequency: "daily",
      minute: 0,
      hour: 9,
    });
  });

  it("parses the weekdays preset (range form) into a day set", () => {
    expect(cronToScheduleBuilder("0 9 * * 1-5")).toMatchObject({
      frequency: "weekly",
      minute: 0,
      hour: 9,
      daysOfWeek: [1, 2, 3, 4, 5],
    });
  });

  it("parses a weekly preset with a single day", () => {
    expect(cronToScheduleBuilder("0 9 * * 1")).toMatchObject({
      frequency: "weekly",
      daysOfWeek: [1],
    });
  });

  it("parses a comma list and normalizes 7 to 0 (Sunday)", () => {
    expect(cronToScheduleBuilder("0 9 * * 7,3")).toMatchObject({
      frequency: "weekly",
      daysOfWeek: [0, 3],
    });
  });

  it("parses the monthly preset", () => {
    expect(cronToScheduleBuilder("0 9 1 * *")).toMatchObject({
      frequency: "monthly",
      minute: 0,
      hour: 9,
      dayOfMonth: 1,
    });
  });

  it("returns null for stepped/complex expressions (custom)", () => {
    expect(cronToScheduleBuilder("*/5 * * * *")).toBeNull();
    expect(cronToScheduleBuilder("0 9 * 1 *")).toBeNull(); // constrained month
    expect(cronToScheduleBuilder("0 9-17 * * *")).toBeNull(); // hour range
    expect(cronToScheduleBuilder("0 9 15 * 1")).toBeNull(); // dom AND dow
    expect(cronToScheduleBuilder("not a cron")).toBeNull();
  });

  it("round-trips every supported preset", () => {
    for (const expression of [
      "0 * * * *",
      "0 9 * * *",
      "0 9 * * 1",
      "0 9 1 * *",
    ]) {
      const builder = cronToScheduleBuilder(expression);
      expect(builder).not.toBeNull();
      expect(scheduleBuilderToCron(builder as ScheduleBuilderState)).toBe(
        expression,
      );
    }
  });
});

describe("time conversion", () => {
  it("converts 24h to 12h", () => {
    expect(to12Hour(0)).toEqual({ hour12: 12, meridiem: "AM" });
    expect(to12Hour(9)).toEqual({ hour12: 9, meridiem: "AM" });
    expect(to12Hour(12)).toEqual({ hour12: 12, meridiem: "PM" });
    expect(to12Hour(23)).toEqual({ hour12: 11, meridiem: "PM" });
  });

  it("converts 12h back to 24h", () => {
    expect(to24Hour(12, "AM")).toBe(0);
    expect(to24Hour(9, "AM")).toBe(9);
    expect(to24Hour(12, "PM")).toBe(12);
    expect(to24Hour(11, "PM")).toBe(23);
  });
});

describe("scheduleBuildersEquivalent", () => {
  it("ignores fields not relevant to the frequency", () => {
    const a: ScheduleBuilderState = {
      frequency: "daily",
      minute: 0,
      hour: 9,
      daysOfWeek: [3],
      dayOfMonth: 15,
    };
    const b: ScheduleBuilderState = {
      frequency: "daily",
      minute: 0,
      hour: 9,
      daysOfWeek: [1],
      dayOfMonth: 1,
    };
    expect(scheduleBuildersEquivalent(a, b)).toBe(true);
  });

  it("distinguishes different weekly day sets", () => {
    const base: ScheduleBuilderState = {
      frequency: "weekly",
      minute: 0,
      hour: 9,
      daysOfWeek: [1, 2],
      dayOfMonth: 1,
    };
    expect(
      scheduleBuildersEquivalent(base, { ...base, daysOfWeek: [1, 3] }),
    ).toBe(false);
  });
});
