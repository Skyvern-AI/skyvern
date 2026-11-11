import type { BeforeSendFn } from "posthog-js";

type ExceptionEntry = {
  value?: unknown;
  type?: unknown;
};

const NOISE_PATTERNS: ReadonlyArray<RegExp> = [
  /^ResizeObserver loop /,
  // Clerk's SDK turns any transport-level failure reaching its Frontend API
  // into a plain Error, so once exception messages are redacted PostHog is left
  // with the bare "Error" type and five minified SDK frames. The same failure
  // reaches the browser log sink with its message and request URL intact, which
  // is where this stays diagnosable.
  /^ClerkJS: Network error at /,
];

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
