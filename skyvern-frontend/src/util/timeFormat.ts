function basicTimeFormat(time: string): string {
  const date = new Date(time);
  const dateString = date.toLocaleDateString("en-US", {
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
  });
  const timeString = date.toLocaleTimeString("en-US");
  return `${dateString} at ${timeString}`;
}

function timeFormatWithShortDate(time: string): string {
  const date = new Date(time);
  const dateString =
    date.getMonth() + 1 + "/" + date.getDate() + "/" + date.getFullYear();
  const timeString = date.toLocaleTimeString("en-US");
  return `${dateString} at ${timeString}`;
}

export { basicTimeFormat, timeFormatWithShortDate };
