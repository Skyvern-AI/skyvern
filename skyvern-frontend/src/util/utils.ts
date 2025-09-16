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
