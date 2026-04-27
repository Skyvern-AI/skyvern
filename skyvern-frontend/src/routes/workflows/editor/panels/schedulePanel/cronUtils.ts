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
