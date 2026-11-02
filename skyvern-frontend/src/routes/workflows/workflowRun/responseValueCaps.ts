import { isRecord } from "@/util/utils";

// Mirrors skyvern/forge/sdk/workflow/response_value_caps.py.
const TRUNCATION_REASON = "exceeded_max_run_response_value_size";
const TRUNCATION_MARKER_KEY = "_truncated";
// The backend trims at most four levels deep, so a marker sits within a few levels of the root.
const MAX_CAP_SEARCH_DEPTH = 8;

export function isTruncationMarker(
  value: unknown,
): value is Record<string, unknown> {
  return (
    isRecord(value) &&
    value.truncated === true &&
    value.reason === TRUNCATION_REASON
  );
}

export function isCappedValue(value: unknown, depth: number = 0): boolean {
  if (depth > MAX_CAP_SEARCH_DEPTH || typeof value !== "object" || !value) {
    return false;
  }
  if (Array.isArray(value)) {
    return value.some((item) => isCappedValue(item, depth + 1));
  }
  return (
    isTruncationMarker(value) ||
    TRUNCATION_MARKER_KEY in value ||
    Object.values(value).some((item) => isCappedValue(item, depth + 1))
  );
}
