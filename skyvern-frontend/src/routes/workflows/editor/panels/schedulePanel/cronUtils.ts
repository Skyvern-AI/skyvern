import cronstrue from "cronstrue";
import { CronExpressionParser } from "cron-parser";

export const CRON_PRESETS = [
  { label: "Hourly", expression: "0 * * * *" },
  { label: "Daily", expression: "0 9 * * *" },
  { label: "Weekdays", expression: "0 9 * * 1-5" },
  { label: "Weekly", expression: "0 9 * * 1" },
  { label: "Monthly", expression: "0 9 1 * *" },
] as const;

export function cronToHumanReadable(expression: string): string {
  try {
    return cronstrue.toString(expression, {
      use24HourTimeFormat: false,
      verbose: false,
    });
  } catch {
    return "Invalid expression";
  }
}

export function isValidCron(expression: string): boolean {
  try {
    CronExpressionParser.parse(expression);
    return true;
  } catch {
    return false;
  }
}

// Mirror of the backend MIN_CRON_INTERVAL_SECONDS guard so the UI can reject
// too-frequent schedules before submitting (and avoid a bare 400 with no
// inline preview feedback).
export const MIN_CRON_INTERVAL_SECONDS = 5 * 60;
// Sample a full day of firings (matching the backend) so a tight cluster outside
// a small fixed window can't slip past, e.g. "0,5,...,55,59 * * * *" hiding the
// 55->59 / 59->00 gaps. 25h covers any minute/hour cycle; the cap bounds "*/1".
const CRON_INTERVAL_SAMPLE_WINDOW_SECONDS = 25 * 60 * 60;
const CRON_INTERVAL_MAX_SAMPLES = 2000;

export function meetsMinCronInterval(
  expression: string,
  minimumIntervalSeconds: number = MIN_CRON_INTERVAL_SECONDS,
): boolean {
  try {
    const interval = CronExpressionParser.parse(expression);
    const runs: number[] = [];
    runs.push(interval.next().toDate().getTime());
    while (runs.length < CRON_INTERVAL_MAX_SAMPLES) {
      runs.push(interval.next().toDate().getTime());
      if (
        (runs[runs.length - 1]! - runs[0]!) / 1000 >=
        CRON_INTERVAL_SAMPLE_WINDOW_SECONDS
      ) {
        break;
      }
    }
    let minGapSeconds = Infinity;
    for (let i = 0; i < runs.length - 1; i++) {
      minGapSeconds = Math.min(minGapSeconds, (runs[i + 1]! - runs[i]!) / 1000);
    }
    return minGapSeconds >= minimumIntervalSeconds;
  } catch {
    return false;
  }
}

export function getNextRuns(
  expression: string,
  timezone: string,
  count: number = 5,
): Date[] {
  try {
    const interval = CronExpressionParser.parse(expression, {
      tz: timezone,
    });

    const runs: Date[] = [];
    for (let i = 0; i < count; i++) {
      runs.push(interval.next().toDate());
    }
    return runs;
  } catch {
    return [];
  }
}

export function formatNextRun(date: Date, timezone: string): string {
  return new Intl.DateTimeFormat("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
    timeZone: timezone,
  }).format(date);
}

export function getTimezones(): string[] {
  try {
    // Intl.supportedValuesOf is available in modern browsers
    return (
      Intl as unknown as { supportedValuesOf: (key: string) => string[] }
    ).supportedValuesOf("timeZone");
  } catch {
    // Fallback for older browsers
    return [
      "America/New_York",
      "America/Chicago",
      "America/Denver",
      "America/Los_Angeles",
      "America/Anchorage",
      "Pacific/Honolulu",
      "Europe/London",
      "Europe/Paris",
      "Europe/Berlin",
      "Asia/Tokyo",
      "Asia/Shanghai",
      "Asia/Singapore",
      "Asia/Kolkata",
      "Australia/Sydney",
      "Pacific/Auckland",
      "UTC",
    ];
  }
}

export function getLocalTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}
