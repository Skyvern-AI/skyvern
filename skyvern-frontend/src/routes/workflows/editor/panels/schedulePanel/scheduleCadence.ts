import type {
  OrganizationScheduleItem,
  WorkflowSchedule,
} from "@/routes/workflows/types/scheduleTypes";
import {
  MIN_SCHEDULE_INTERVAL_SECONDS,
  cronToHumanReadable,
  getNextRuns,
  isValidCron,
  meetsMinCronInterval,
} from "./cronUtils";

export type IntervalUnit = "minutes" | "hours" | "days";

export type IntervalDraft = {
  // Raw text of the length field, so clearing it reads as empty rather than 0.
  every: string;
  unit: IntervalUnit;
  // A datetime-local value read as wall-clock time in the schedule's timezone, or "" to let the server pick.
  firstFireAt: string;
};

export type IntervalDraftErrors = {
  every: string | null;
  firstFireAt: string | null;
};

export type ScheduleCadence = Pick<
  WorkflowSchedule,
  "cron_expression" | "interval_seconds" | "first_fire_at"
>;

export type CadencePayload =
  | { cron_expression: string }
  | { interval_seconds: number; first_fire_at?: string };

export const DEFAULT_INTERVAL_DRAFT: IntervalDraft = {
  every: "1",
  unit: "days",
  firstFireAt: "",
};

const UNIT_SECONDS: Record<IntervalUnit, number> = {
  minutes: 60,
  hours: 60 * 60,
  days: 24 * 60 * 60,
};

const DAY_MS = 24 * 60 * 60 * 1000;

// Mirrors the backend's MIN_FIRST_FIRE_LEAD: an explicit first run must be at least this far out.
const MIN_FIRST_FIRE_LEAD_MS = 60_000;
// A duplicate's anchor must still clear that lead when the server receives it.
const DUPLICATE_ANCHOR_LEAD_MS = MIN_FIRST_FIRE_LEAD_MS + 30_000;

export function intervalDraftSeconds(draft: IntervalDraft): number {
  if (draft.every.trim() === "") return Number.NaN;
  return Math.round(Number(draft.every) * UNIT_SECONDS[draft.unit]);
}

export function intervalDraftFromSeconds(seconds: number): IntervalDraft {
  const unit: IntervalUnit =
    seconds % UNIT_SECONDS.days === 0
      ? "days"
      : seconds % UNIT_SECONDS.hours === 0
        ? "hours"
        : "minutes";
  return {
    every: String(seconds / UNIT_SECONDS[unit]),
    unit,
    firstFireAt: "",
  };
}

function timezoneOffsetMs(instant: number, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(instant));
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    Number(parts.find((p) => p.type === type)?.value);
  const wallAsUtc = Date.UTC(
    part("year"),
    part("month") - 1,
    part("day"),
    part("hour"),
    part("minute"),
    part("second"),
  );
  return wallAsUtc - Math.floor(instant / 1000) * 1000;
}

export function zonedWallTimeToDate(wallTime: string, timeZone: string): Date {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(wallTime);
  if (!match) return new Date(Number.NaN);
  const [year, month, day, hour, minute] = match.slice(1).map(Number) as [
    number,
    number,
    number,
    number,
    number,
  ];
  const wallAsUtc = Date.UTC(year, month - 1, day, hour, minute);
  // The offsets a day either side bracket any DST change: a skipped wall time round-trips under neither,
  // and a repeated one round-trips under both, where the earlier instant is its first occurrence.
  const candidates = [wallAsUtc - DAY_MS, wallAsUtc + DAY_MS]
    .map((probe) => wallAsUtc - timezoneOffsetMs(probe, timeZone))
    .filter(
      (instant) => instant + timezoneOffsetMs(instant, timeZone) === wallAsUtc,
    );
  return new Date(candidates.length > 0 ? Math.min(...candidates) : Number.NaN);
}

export function intervalDraftErrors(
  draft: IntervalDraft,
  timezone: string,
  now: number = Date.now(),
): IntervalDraftErrors {
  const seconds = intervalDraftSeconds(draft);
  let every: string | null = null;
  if (!Number.isFinite(seconds) || seconds <= 0) {
    every = "Enter how often the schedule runs.";
  } else if (seconds < MIN_SCHEDULE_INTERVAL_SECONDS) {
    every = "Schedule runs must be at least 5 minutes apart.";
  }
  let firstFireAt: string | null = null;
  if (draft.firstFireAt) {
    const instant = zonedWallTimeToDate(draft.firstFireAt, timezone).getTime();
    if (Number.isNaN(instant)) {
      firstFireAt = `That time does not exist in ${timezone} because of a daylight saving change.`;
    } else if (instant < now + MIN_FIRST_FIRE_LEAD_MS) {
      firstFireAt = "First run must be at least a minute from now.";
    }
  }
  return { every, firstFireAt };
}

function intervalLength(seconds: number): [number, string] {
  // "Every day" would read as a fixed time of day, which a 24-hour interval drifts off across DST.
  if (seconds % 86400 === 0 && seconds > 86400) return [seconds / 86400, "day"];
  if (seconds % 3600 === 0) return [seconds / 3600, "hour"];
  if (seconds % 60 === 0) return [seconds / 60, "minute"];
  return [seconds, "second"];
}

export function formatIntervalDuration(seconds: number): string {
  const [count, unit] = intervalLength(seconds);
  return `${count} ${unit}${count === 1 ? "" : "s"}`;
}

export function formatInterval(seconds: number): string {
  const [count, unit] = intervalLength(seconds);
  return count === 1 ? `Every ${unit}` : `Every ${count} ${unit}s`;
}

// Mirrors the backend helper: the first tick strictly after now on the anchor + k * interval grid.
export function getIntervalRuns(
  intervalSeconds: number,
  firstFireAt: Date,
  count: number,
  now: number = Date.now(),
): Date[] {
  const anchor = firstFireAt.getTime();
  const step = intervalSeconds * 1000;
  const first = now < anchor ? 0 : Math.floor((now - anchor) / step) + 1;
  return Array.from(
    { length: count },
    (_, i) => new Date(anchor + (first + i) * step),
  );
}

export function upcomingFirstRun(
  firstFireAt: string | null,
  now: number = Date.now(),
): Date | null {
  const firstRun = firstFireAt ? new Date(firstFireAt) : null;
  return firstRun && firstRun.getTime() > now ? firstRun : null;
}

export function describeCadence(schedule: ScheduleCadence): string {
  if (schedule.interval_seconds) {
    return formatInterval(schedule.interval_seconds);
  }
  return cronToHumanReadable(schedule.cron_expression ?? "");
}

export function getCadenceNextRuns(
  schedule: ScheduleCadence,
  timezone: string,
  count: number,
): Date[] {
  if (schedule.interval_seconds && schedule.first_fire_at) {
    return getIntervalRuns(
      schedule.interval_seconds,
      new Date(schedule.first_fire_at),
      count,
    );
  }
  return schedule.cron_expression
    ? getNextRuns(schedule.cron_expression, timezone, count)
    : [];
}

export function cronBelowMinInterval(cronExpression: string | null): boolean {
  return (
    cronExpression !== null &&
    isValidCron(cronExpression) &&
    !meetsMinCronInterval(cronExpression)
  );
}

export function isCadenceAccepted(
  cronExpression: string,
  interval: IntervalDraft | null,
  timezone: string,
): boolean {
  if (interval) {
    const errors = intervalDraftErrors(interval, timezone);
    return errors.every === null && errors.firstFireAt === null;
  }
  return isValidCron(cronExpression) && meetsMinCronInterval(cronExpression);
}

export function buildCadencePayload(
  cronExpression: string,
  interval: IntervalDraft | null,
  timezone: string,
): CadencePayload {
  if (!interval) {
    return { cron_expression: cronExpression };
  }
  return {
    interval_seconds: intervalDraftSeconds(interval),
    ...(interval.firstFireAt && {
      first_fire_at: zonedWallTimeToDate(
        interval.firstFireAt,
        timezone,
      ).toISOString(),
    }),
  };
}

export function buildDuplicateSchedulePayload(
  schedule: OrganizationScheduleItem,
  now: number = Date.now(),
) {
  const cadence: CadencePayload =
    schedule.interval_seconds && schedule.first_fire_at
      ? {
          interval_seconds: schedule.interval_seconds,
          first_fire_at: getIntervalRuns(
            schedule.interval_seconds,
            new Date(schedule.first_fire_at),
            1,
            now + DUPLICATE_ANCHOR_LEAD_MS,
          )[0]!.toISOString(),
        }
      : { cron_expression: schedule.cron_expression ?? "" };
  return {
    ...cadence,
    timezone: schedule.timezone,
    enabled: schedule.enabled,
    parameters: schedule.parameters,
    name: `${schedule.name ?? schedule.workflow_title} (copy)`,
  };
}
