/**
 * Format a workflow parameter default value for display.
 *
 * Fixes SKY-8785: JSON/object defaults previously rendered as "[object Object]"
 * because of implicit String() coercion. Objects and arrays are now serialized
 * with JSON.stringify; strings are returned unchanged, and numbers/booleans
 * are rendered with String() rather than JSON quoting.
 */
export function formatDefaultValue(value: unknown): string {
  if (value == null) return String(value);
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean")
    return String(value);
  try {
    // JSON.stringify returns undefined for functions/symbols/etc; fall back.
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}
