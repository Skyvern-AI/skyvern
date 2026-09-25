import { describe, expect, it } from "vitest";
import type { OrganizationScheduleItem } from "@/routes/workflows/types/scheduleTypes";
import {
  buildCadencePayload,
  buildDuplicateSchedulePayload,
  formatInterval,
  getIntervalRuns,
  intervalDraftErrors,
  intervalDraftFromSeconds,
  upcomingFirstRun,
  zonedWallTimeToDate,
} from "./scheduleCadence";

const NOW = Date.parse("2026-11-01T12:00:00Z");

function orgSchedule(
  overrides: Partial<OrganizationScheduleItem>,
): OrganizationScheduleItem {
  return {
    workflow_schedule_id: "wfs_1",
    organization_id: "o_1",
    workflow_permanent_id: "wpid_1",
    workflow_title: "Report",
    cron_expression: null,
    interval_seconds: null,
    first_fire_at: null,
    timezone: "America/Los_Angeles",
    enabled: false,
    parameters: { city: "Toronto" },
    name: "Every 3 days",
    description: null,
    next_run: null,
    created_at: "2026-10-01T00:00:00",
    modified_at: "2026-10-01T00:00:00",
    ...overrides,
  };
}

describe("buildCadencePayload", () => {
  it("sends interval_seconds and no cron in interval mode", () => {
    expect(
      buildCadencePayload(
        "0 9 * * *",
        { every: "72", unit: "hours", firstFireAt: "" },
        "UTC",
      ),
    ).toEqual({ interval_seconds: 259200 });
  });

  it("reads the first run as wall-clock time in the schedule's timezone", () => {
    const draft = { every: "5", unit: "hours", firstFireAt: "" } as const;
    expect(
      buildCadencePayload(
        "",
        { ...draft, firstFireAt: "2026-11-01T01:30" },
        "America/New_York",
      ),
    ).toEqual({
      interval_seconds: 18000,
      first_fire_at: "2026-11-01T05:30:00.000Z",
    });
    expect(
      buildCadencePayload(
        "",
        { ...draft, firstFireAt: "2026-11-03T09:00" },
        "Asia/Kolkata",
      ),
    ).toEqual({
      interval_seconds: 18000,
      first_fire_at: "2026-11-03T03:30:00.000Z",
    });
  });

  it("sends only the cron in cron mode", () => {
    expect(buildCadencePayload("0 9 * * *", null, "UTC")).toEqual({
      cron_expression: "0 9 * * *",
    });
  });
});

describe("intervalDraftErrors", () => {
  it("puts each error on its own field", () => {
    expect(
      intervalDraftErrors(
        { every: "5", unit: "hours", firstFireAt: "2026-10-01T09:00" },
        "UTC",
        NOW,
      ),
    ).toEqual({
      every: null,
      firstFireAt: "First run must be at least a minute from now.",
    });
    expect(
      intervalDraftErrors({ every: "", unit: "hours", firstFireAt: "" }, "UTC"),
    ).toEqual({
      every: "Enter how often the schedule runs.",
      firstFireAt: null,
    });
  });

  it("rejects a first run inside the server's one-minute lead", () => {
    const draft = { every: "5", unit: "hours" } as const;
    expect(
      intervalDraftErrors(
        { ...draft, firstFireAt: "2026-11-01T12:00:30" },
        "UTC",
        NOW - 1_000,
      ).firstFireAt,
    ).toBe("First run must be at least a minute from now.");
    expect(
      intervalDraftErrors(
        { ...draft, firstFireAt: "2026-11-01T12:02" },
        "UTC",
        NOW,
      ).firstFireAt,
    ).toBe(null);
  });

  it("judges the first run in the schedule's timezone, not the browser's", () => {
    const draft = {
      every: "5",
      unit: "hours",
      firstFireAt: "2026-11-01T13:00",
    } as const;
    expect(
      intervalDraftErrors(draft, "America/New_York", NOW).firstFireAt,
    ).toBe(null);
    expect(intervalDraftErrors(draft, "Europe/Berlin", NOW).firstFireAt).toBe(
      "First run must be at least a minute from now.",
    );
  });
});

describe("zonedWallTimeToDate", () => {
  it.each([
    ["America/Los_Angeles", "2026-11-01T01:30", "2026-11-01T08:30:00.000Z"],
    ["Europe/Berlin", "2026-10-25T02:30", "2026-10-25T00:30:00.000Z"],
    ["America/Los_Angeles", "2026-07-01T09:00", "2026-07-01T16:00:00.000Z"],
  ])("resolves %s %s to its first occurrence", (timeZone, wall, iso) => {
    expect(zonedWallTimeToDate(wall, timeZone).toISOString()).toBe(iso);
  });

  it("rejects a wall time skipped by the spring-forward gap", () => {
    const draft = {
      every: "5",
      unit: "hours",
      firstFireAt: "2027-03-14T02:30",
    } as const;
    expect(
      Number.isNaN(
        zonedWallTimeToDate(draft.firstFireAt, "America/Los_Angeles").getTime(),
      ),
    ).toBe(true);
    expect(
      intervalDraftErrors(draft, "America/Los_Angeles", NOW).firstFireAt,
    ).toBe(
      "That time does not exist in America/Los_Angeles because of a daylight saving change.",
    );
  });
});

describe("intervalDraftFromSeconds", () => {
  it.each([
    [259200, "3", "days"],
    [18000, "5", "hours"],
    [5400, "90", "minutes"],
  ])("shows %i seconds as %s %s", (seconds, every, unit) => {
    expect(intervalDraftFromSeconds(seconds)).toMatchObject({ every, unit });
  });
});

describe("formatInterval", () => {
  it.each([
    [86400, "Every 24 hours"],
    [259200, "Every 3 days"],
    [3600, "Every hour"],
    [18000, "Every 5 hours"],
    [5400, "Every 90 minutes"],
    [301, "Every 301 seconds"],
  ])("formats %i seconds as %s", (seconds, label) => {
    expect(formatInterval(seconds)).toBe(label);
  });
});

describe("getIntervalRuns", () => {
  it("returns the anchor grid ticks strictly after now", () => {
    const runs = getIntervalRuns(
      5 * 3600,
      new Date("2026-10-31T20:00:00Z"),
      2,
      NOW,
    );
    expect(runs.map((run) => run.toISOString())).toEqual([
      "2026-11-01T16:00:00.000Z",
      "2026-11-01T21:00:00.000Z",
    ]);
  });
});

describe("upcomingFirstRun", () => {
  it("shows the first run only while it is in the future", () => {
    expect(upcomingFirstRun("2026-11-01T12:00:01Z", NOW)).toEqual(
      new Date("2026-11-01T12:00:01Z"),
    );
    expect(upcomingFirstRun("2026-11-01T12:00:00Z", NOW)).toBeNull();
    expect(upcomingFirstRun(null, NOW)).toBeNull();
  });
});

describe("buildDuplicateSchedulePayload", () => {
  it("copies an interval schedule onto the same grid from its next future tick", () => {
    expect(
      buildDuplicateSchedulePayload(
        orgSchedule({
          interval_seconds: 259200,
          first_fire_at: "2026-10-30T15:00:00Z",
        }),
        NOW,
      ),
    ).toEqual({
      interval_seconds: 259200,
      first_fire_at: "2026-11-02T15:00:00.000Z",
      timezone: "America/Los_Angeles",
      enabled: false,
      parameters: { city: "Toronto" },
      name: "Every 3 days (copy)",
    });
  });

  it("skips to the following tick when the next one is under 30 seconds away", () => {
    expect(
      buildDuplicateSchedulePayload(
        orgSchedule({
          interval_seconds: 18000,
          first_fire_at: "2026-11-01T02:00:10Z",
        }),
        NOW,
      ),
    ).toMatchObject({ first_fire_at: "2026-11-01T17:00:10.000Z" });
  });

  it("copies a cron schedule's cron and nothing interval-shaped", () => {
    const payload = buildDuplicateSchedulePayload(
      orgSchedule({ cron_expression: "0 9 * * 1" }),
      NOW,
    );
    expect(payload).toMatchObject({ cron_expression: "0 9 * * 1" });
    expect(payload).not.toHaveProperty("interval_seconds");
    expect(payload).not.toHaveProperty("first_fire_at");
  });
});
