function normalizeUtcTimestamp(time: string): string {
  // Adjust the fractional seconds to milliseconds (3 digits)
  time = time.replace(/\.(\d{3})\d*/, ".$1");

  // Timezone-less timestamps are UTC (the backend serializes naive UTC
  // datetimes); append 'Z' unless an offset is already present.
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/.test(time)) {
    time += "Z";
  }

  return time;
}

function basicLocalTimeFormat(time: string): string {
  time = normalizeUtcTimestamp(time);

  const date = new Date(time);
  const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

  // Format the date and time in the local time zone
  const dateString = date.toLocaleDateString("en-US", {
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: localTimezone,
  });
  const timeString = date.toLocaleTimeString("en-US", {
    timeZone: localTimezone,
  });

  return `${dateString} at ${timeString}`;
}

const compactDateFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
});
const compactDateWithYearFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
});
const compactTimeFormat = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "2-digit",
});

function compactLocalDateTime(time: string): string {
  time = normalizeUtcTimestamp(time);

  const date = new Date(time);
  if (isNaN(date.getTime())) {
    return "";
  }
  const sameYear = date.getFullYear() === new Date().getFullYear();

  const dateString = (
    sameYear ? compactDateFormat : compactDateWithYearFormat
  ).format(date);
  const timeString = compactTimeFormat.format(date);

  return `${dateString}, ${timeString}`;
}

function basicTimeFormat(time: string): string {
  time = normalizeUtcTimestamp(time);

  const date = new Date(time);

  // Render in UTC to match the "UTC" label; this is the hover companion to
  // basicLocalTimeFormat, which shows the same instant in the local time zone.
  const dateString = date.toLocaleDateString("en-US", {
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
  const timeString = date.toLocaleTimeString("en-US", {
    timeZone: "UTC",
  });
  return `${dateString} at ${timeString} UTC`;
}

function timeFormatWithShortDate(time: string): string {
  const date = new Date(time);
  const dateString =
    date.getMonth() + 1 + "/" + date.getDate() + "/" + date.getFullYear();
  const timeString = date.toLocaleTimeString("en-US");
  return `${dateString} at ${timeString} UTC`;
}

function localTimeFormatWithShortDate(time: string): string {
  time = normalizeUtcTimestamp(time);

  const date = new Date(time);
  const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

  const dateString =
    date.getMonth() + 1 + "/" + date.getDate() + "/" + date.getFullYear();

  const timeString = date.toLocaleTimeString("en-US", {
    timeZone: localTimezone,
  });

  return `${dateString} at ${timeString}`;
}

function formatTimeRemaining(seconds: number): string {
  if (seconds <= 0) return "0:00";
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

function formatExecutionTime(
  createdAt: string,
  finishedAt: string | null,
): string | null {
  if (!finishedAt) {
    return null;
  }
  const start = new Date(normalizeUtcTimestamp(createdAt));
  const end = new Date(normalizeUtcTimestamp(finishedAt));
  if (isNaN(start.getTime()) || isNaN(end.getTime())) {
    return null;
  }
  return formatSeconds((end.getTime() - start.getTime()) / 1000);
}

// Keeps the two largest units, so a 26h span reads "1d 2h".
function formatSeconds(seconds: number): string {
  const sec = Math.max(0, Math.round(seconds));
  if (sec < 60) {
    return `${sec}s`;
  }
  if (sec < 3600) {
    return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  }
  if (sec < 86400) {
    return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
  }
  return `${Math.floor(sec / 86400)}d ${Math.floor((sec % 86400) / 3600)}h`;
}

function formatDurationSeconds(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0 || !Number.isFinite(seconds)) {
    return "—";
  }

  return formatSeconds(seconds);
}

export {
  normalizeUtcTimestamp,
  basicLocalTimeFormat,
  compactLocalDateTime,
  basicTimeFormat,
  timeFormatWithShortDate,
  localTimeFormatWithShortDate,
  formatTimeRemaining,
  formatExecutionTime,
  formatDurationSeconds,
  formatSeconds,
};
