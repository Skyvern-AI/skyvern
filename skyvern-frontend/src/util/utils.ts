import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const clampToZero = (n: number) => Math.max(n, 0);

export const formatMs = (elapsed: number) => {
  let seconds = clampToZero(Math.floor(elapsed / 1000));
  let minutes = clampToZero(Math.floor(seconds / 60));
  let hours = clampToZero(Math.floor(minutes / 60));
  const days = clampToZero(Math.floor(hours / 24));

  seconds = seconds % 60;
  minutes = minutes % 60;
  hours = hours % 24;

  const ago =
    days === 0 && hours === 0 && minutes === 0 && seconds === 0
      ? "now"
      : days === 0 && hours === 0 && minutes === 0
        ? `${seconds}s ago`
        : days === 0 && hours === 0
          ? `${minutes}m ago`
          : days === 0
            ? `${hours}h ago`
            : `${days}d ago`;

  return {
    ago,
    hour: hours,
    minute: minutes,
    second: seconds,
    day: days,
  };
};

export function toDate(
  time: string,
  defaultDate: Date | null = new Date(0),
): Date | null {
  time = time.replace(/\.(\d{3})\d*/, ".$1");

  if (!time.endsWith("Z")) {
    time += "Z";
  }

  const date = new Date(time);

  if (isNaN(date.getTime())) {
    return defaultDate;
  }

  return date;
}

/** Returns a date in the format 'July 14th at 4:52pm' */
export function formatDate(date: Date): string {
  const options: Intl.DateTimeFormatOptions = {
    month: "long",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
    hour12: true,
  };
  return date.toLocaleString("en-US", options);
}

/**
 * Handle infinite scroll logic for loading more items
 * @param event - React scroll event
 * @param fetchNextPage - Function to fetch next page
 * @param hasNextPage - Whether there are more pages to fetch
 * @param isFetchingNextPage - Whether currently fetching
 * @param scrollThreshold - Percentage threshold to trigger fetch (default 0.8)
 */
export const handleInfiniteScroll = (
  event: React.UIEvent<HTMLDivElement>,
  fetchNextPage: () => void,
  hasNextPage: boolean,
  isFetchingNextPage: boolean,
  scrollThreshold: number = 0.8,
) => {
  const target = event.currentTarget;
  const scrollPercentage =
    (target.scrollTop + target.clientHeight) / target.scrollHeight;

  if (
    scrollPercentage >= scrollThreshold &&
    hasNextPage &&
    !isFetchingNextPage
  ) {
    fetchNextPage();
  }
};
