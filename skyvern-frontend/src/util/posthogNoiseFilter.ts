import type { BeforeSendFn } from "posthog-js";

type ExceptionEntry = {
  value?: unknown;
  type?: unknown;
};

const NOISE_PATTERNS: ReadonlyArray<RegExp> = [/^ResizeObserver loop /];

function isNoiseException(entry: ExceptionEntry): boolean {
  const value = entry?.value;
  if (typeof value !== "string") return false;
  return NOISE_PATTERNS.some((pattern) => pattern.test(value));
}

export const dropNoiseExceptions: BeforeSendFn = (event) => {
  if (!event || event.event !== "$exception") return event;
  const list = (event.properties?.$exception_list ?? []) as ExceptionEntry[];
  if (Array.isArray(list) && list.some(isNoiseException)) {
    return null;
  }
  return event;
};
