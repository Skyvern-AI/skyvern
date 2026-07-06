export type RunWindow = {
  createdAtStart?: string;
  createdAtEnd?: string;
};

const PRESET_DAYS: Record<string, number> = {
  "7d": 7,
  "30d": 30,
  "90d": 90,
  "365d": 365,
};

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function isValidIsoDate(value: string): boolean {
  if (!ISO_DATE_RE.test(value)) {
    return false;
  }
  const parsed = new Date(`${value}T00:00:00Z`);
  return (
    !Number.isNaN(parsed.getTime()) &&
    parsed.toISOString().slice(0, 10) === value
  );
}

/**
 * Derives the runs-table created-at window from the shared ?period= URL contract.
 *
 * Returns {} when no period is set so pure-OSS pages list all runs.
 */
export function resolveRunWindow(
  searchParams: URLSearchParams,
  now: Date = new Date(),
): RunWindow {
  const period = searchParams.get("period");
  if (!period) {
    return {};
  }

  if (period === "custom") {
    const from = searchParams.get("from") ?? "";
    const to = searchParams.get("to") ?? "";
    if (!isValidIsoDate(from) || !isValidIsoDate(to) || from > to) {
      return {};
    }
    const end = new Date(`${to}T00:00:00Z`);
    end.setUTCDate(end.getUTCDate() + 1); // inclusive of the whole `to` day
    return {
      createdAtStart: new Date(`${from}T00:00:00Z`).toISOString(),
      createdAtEnd: end.toISOString(),
    };
  }

  const days = PRESET_DAYS[period];
  if (!days) {
    return {};
  }
  const start = new Date(now);
  start.setUTCHours(0, 0, 0, 0);
  start.setUTCDate(start.getUTCDate() - days);
  return { createdAtStart: start.toISOString() };
}
