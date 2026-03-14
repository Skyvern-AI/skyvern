function normalizeUtcTimestamp(time: string): string {
  // Adjust the fractional seconds to milliseconds (3 digits)
  time = time.replace(/\.(\d{3})\d*/, ".$1");

  // Append 'Z' to indicate UTC time if not already present
  if (!time.endsWith("Z")) {
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

function basicTimeFormat(time: string): string {
  const date = new Date(time);
  const dateString = date.toLocaleDateString("en-US", {
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
  });
  const timeString = date.toLocaleTimeString("en-US");
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
  const totalSeconds = Math.max(
    0,
    Math.round((end.getTime() - start.getTime()) / 1000),
  );
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  }
  if (minutes > 0) {
    return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`;
  }
  return `${seconds}s`;
}

export {
  basicLocalTimeFormat,
  basicTimeFormat,
  timeFormatWithShortDate,
  localTimeFormatWithShortDate,
  formatTimeRemaining,
  formatExecutionTime,
};
