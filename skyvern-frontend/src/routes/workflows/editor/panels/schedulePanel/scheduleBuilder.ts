export type ScheduleFrequency = "hourly" | "daily" | "weekly" | "monthly";

export type ScheduleBuilderState = {
  frequency: ScheduleFrequency;
  minute: number;
  hour: number;
  daysOfWeek: number[];
  dayOfMonth: number;
};

export const DEFAULT_SCHEDULE_BUILDER: ScheduleBuilderState = {
  frequency: "daily",
  minute: 0,
  hour: 9,
  daysOfWeek: [1],
  dayOfMonth: 1,
};

export const DAY_OF_WEEK_OPTIONS: ReadonlyArray<{
  value: number;
  short: string;
  label: string;
}> = [
  { value: 0, short: "S", label: "Sunday" },
  { value: 1, short: "M", label: "Monday" },
  { value: 2, short: "T", label: "Tuesday" },
  { value: 3, short: "W", label: "Wednesday" },
  { value: 4, short: "T", label: "Thursday" },
  { value: 5, short: "F", label: "Friday" },
  { value: 6, short: "S", label: "Saturday" },
];

export function to12Hour(hour24: number): {
  hour12: number;
  meridiem: "AM" | "PM";
} {
  const meridiem = hour24 < 12 ? "AM" : "PM";
  const base = hour24 % 12;
  return { hour12: base === 0 ? 12 : base, meridiem };
}

export function to24Hour(hour12: number, meridiem: "AM" | "PM"): number {
  if (meridiem === "AM") {
    return hour12 === 12 ? 0 : hour12;
  }
  return hour12 === 12 ? 12 : hour12 + 12;
}

export function scheduleBuilderToCron(builder: ScheduleBuilderState): string {
  const { frequency, minute, hour, daysOfWeek, dayOfMonth } = builder;
  switch (frequency) {
    case "hourly":
      return `${minute} * * * *`;
    case "daily":
      return `${minute} ${hour} * * *`;
    case "weekly": {
      const days = [...new Set(daysOfWeek)].sort((a, b) => a - b);
      const dow = days.length > 0 ? days.join(",") : "*";
      return `${minute} ${hour} * * ${dow}`;
    }
    case "monthly":
      return `${minute} ${hour} ${dayOfMonth} * *`;
  }
}

function parseSingleInt(
  field: string,
  min: number,
  max: number,
): number | null {
  if (!/^\d+$/.test(field)) return null;
  const value = Number(field);
  if (value < min || value > max) return null;
  return value;
}

// Expands a day-of-week field (single, comma list, and/or a-b ranges; 7 -> 0)
// into a sorted, de-duplicated set. Returns null for steps ("*/2"), "*", or any
// token outside 0-7 so callers fall back to the raw cron ("Custom") path.
function parseDaysOfWeek(field: string): number[] | null {
  if (field === "*" || field.includes("/")) return null;
  const days = new Set<number>();
  for (const token of field.split(",")) {
    const range = token.match(/^(\d+)-(\d+)$/);
    if (range) {
      const start = Number(range[1]);
      const end = Number(range[2]);
      if (start > end) return null;
      for (let d = start; d <= end; d++) {
        if (d < 0 || d > 7) return null;
        days.add(d === 7 ? 0 : d);
      }
      continue;
    }
    if (!/^\d+$/.test(token)) return null;
    const value = Number(token);
    if (value < 0 || value > 7) return null;
    days.add(value === 7 ? 0 : value);
  }
  if (days.size === 0) return null;
  return [...days].sort((a, b) => a - b);
}

// Parses a cron expression into builder state when it maps cleanly onto one of
// the supported recurrence shapes (hourly / daily / weekly / monthly). Returns
// null for anything else so the UI keeps it as a raw "Custom" expression.
export function cronToScheduleBuilder(
  expression: string,
): ScheduleBuilderState | null {
  const parts = expression.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minuteField, hourField, domField, monthField, dowField] = parts as [
    string,
    string,
    string,
    string,
    string,
  ];
  if (monthField !== "*") return null;

  const minute = parseSingleInt(minuteField, 0, 59);
  if (minute === null) return null;

  if (hourField === "*" && domField === "*" && dowField === "*") {
    return { ...DEFAULT_SCHEDULE_BUILDER, frequency: "hourly", minute };
  }

  const hour = parseSingleInt(hourField, 0, 23);
  if (hour === null) return null;

  if (domField === "*" && dowField === "*") {
    return { ...DEFAULT_SCHEDULE_BUILDER, frequency: "daily", minute, hour };
  }

  if (domField === "*" && dowField !== "*") {
    const daysOfWeek = parseDaysOfWeek(dowField);
    if (!daysOfWeek) return null;
    return {
      ...DEFAULT_SCHEDULE_BUILDER,
      frequency: "weekly",
      minute,
      hour,
      daysOfWeek,
    };
  }

  if (dowField === "*" && domField !== "*") {
    const dayOfMonth = parseSingleInt(domField, 1, 31);
    if (dayOfMonth === null) return null;
    return {
      ...DEFAULT_SCHEDULE_BUILDER,
      frequency: "monthly",
      minute,
      hour,
      dayOfMonth,
    };
  }

  return null;
}

// Compares only the fields that matter for a given frequency, so re-parsing a
// generated cron (which resets unshown fields to defaults) doesn't clobber a
// user's earlier selections or spin a render loop.
export function scheduleBuildersEquivalent(
  a: ScheduleBuilderState,
  b: ScheduleBuilderState,
): boolean {
  if (a.frequency !== b.frequency) return false;
  switch (a.frequency) {
    case "hourly":
      return a.minute === b.minute;
    case "daily":
      return a.minute === b.minute && a.hour === b.hour;
    case "weekly": {
      if (a.minute !== b.minute || a.hour !== b.hour) return false;
      const aDays = [...new Set(a.daysOfWeek)].sort((x, y) => x - y);
      const bDays = [...new Set(b.daysOfWeek)].sort((x, y) => x - y);
      return (
        aDays.length === bDays.length &&
        aDays.every((day, index) => day === bDays[index])
      );
    }
    case "monthly":
      return (
        a.minute === b.minute &&
        a.hour === b.hour &&
        a.dayOfMonth === b.dayOfMonth
      );
  }
}
