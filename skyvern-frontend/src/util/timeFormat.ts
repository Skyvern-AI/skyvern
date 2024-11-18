function basicLocalTimeFormat(time: string): string {
  // Adjust the fractional seconds to milliseconds (3 digits)
  time = time.replace(/\.(\d{3})\d*/, ".$1");

  // Append 'Z' to indicate UTC time if not already present
  if (!time.endsWith("Z")) {
    time += "Z";
  }

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
  // Adjust the fractional seconds to milliseconds (3 digits)
  time = time.replace(/\.(\d{3})\d*/, ".$1");

  // Append 'Z' to indicate UTC time if not already present
  if (!time.endsWith("Z")) {
    time += "Z";
  }

  const date = new Date(time);
  const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

  const dateString =
    date.getMonth() + 1 + "/" + date.getDate() + "/" + date.getFullYear();

  const timeString = date.toLocaleTimeString("en-US", {
    timeZone: localTimezone,
  });

  return `${dateString} at ${timeString}`;
}

export {
  basicLocalTimeFormat,
  basicTimeFormat,
  timeFormatWithShortDate,
  localTimeFormatWithShortDate,
};
