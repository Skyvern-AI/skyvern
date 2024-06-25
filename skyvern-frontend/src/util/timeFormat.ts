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

export { basicTimeFormat };
