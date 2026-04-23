/**
 * Validate an errorCodeMapping JSON string. The value must:
 *   - parse as JSON,
 *   - parse to a plain object (not an array, not a primitive, not `null`),
 *   - and every key must not have surrounding whitespace — whitespace-bearing
 *     keys look identical to clean keys in most UIs but do not match at
 *     runtime, so they silently mis-fire (SKY-8825).
 *
 * `"null"` (literal JSON null) is the sentinel for "error mapping disabled"
 * and is treated as valid.
 */
export function validateErrorCodeMapping(
  label: string,
  errorCodeMapping: string,
): Array<string> {
  const errors: Array<string> = [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(errorCodeMapping);
  } catch {
    errors.push(`${label}: Error messages is not valid JSON.`);
    return errors;
  }
  // `null` is the disabled sentinel — valid.
  if (parsed === null) {
    return errors;
  }
  if (typeof parsed !== "object" || Array.isArray(parsed)) {
    errors.push(
      `${label}: Error messages must be a JSON object (got ${Array.isArray(parsed) ? "array" : typeof parsed}).`,
    );
    return errors;
  }
  Object.keys(parsed as Record<string, unknown>).forEach((key) => {
    if (key !== key.trim()) {
      errors.push(
        `${label}: Error messages key "${key}" has surrounding whitespace — remove the whitespace or it will never match at runtime.`,
      );
    }
  });
  return errors;
}
