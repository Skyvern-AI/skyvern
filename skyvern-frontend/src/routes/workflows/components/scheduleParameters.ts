import {
  WorkflowParameterTypes,
  type Parameter,
  type WorkflowParameter,
} from "@/routes/workflows/types/workflowTypes";

/**
 * Mirror of `_is_schedule_input_parameter` in
 * skyvern/forge/sdk/workflow/service.py. Only `WorkflowParameter` instances
 * (parameter_type === "workflow") are user-supplied for schedules.
 */
export function isScheduleParameter(
  parameter: Parameter,
): parameter is WorkflowParameter {
  return parameter.parameter_type === WorkflowParameterTypes.Workflow;
}

/**
 * A workflow parameter is "required" iff it has no default value. The form
 * validators block submission until every required parameter has a value;
 * parameters with defaults can be left blank and the backend will fall back
 * to the default at execution time (see service.py:582-583).
 */
export function isRequired(parameter: WorkflowParameter): boolean {
  return (
    parameter.default_value === null || parameter.default_value === undefined
  );
}

export function hasUserFacingParameters(
  parameters: ReadonlyArray<Parameter>,
): boolean {
  return parameters.some(isScheduleParameter);
}

/**
 * Compute the validation errors for a schedule parameter form. Returns
 * a `{ key: "Required" }` map for every required parameter that is
 * missing a value, mirroring the backend's loop in
 * skyvern/forge/sdk/workflow/service.py:552-607. The caller decides
 * what to do with the result — typically:
 *   const errors = validateScheduleParameters(workflowParameters, values);
 *   setParameterErrors(errors);
 *   if (Object.keys(errors).length > 0) return; // block submit
 */
export function validateScheduleParameters(
  parameters: ReadonlyArray<Parameter>,
  values: Record<string, unknown>,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const parameter of parameters) {
    if (!isScheduleParameter(parameter)) continue;
    const value = values[parameter.key];
    if (isRequired(parameter) && isMissingRequiredValue(parameter, value)) {
      errors[parameter.key] = "Required";
      continue;
    }
    if (
      parameter.workflow_parameter_type === "json" &&
      typeof value === "string" &&
      value.trim() !== ""
    ) {
      try {
        JSON.parse(value);
      } catch {
        errors[parameter.key] = "Invalid JSON";
      }
    }
  }
  return errors;
}

/**
 * Format a stored schedule parameter value for read-only display in the
 * schedule detail page. Plain `String(value)` produces "[object Object]"
 * for `json` parameters with object values and for `file_url` parameters
 * stored as `{ s3uri: "..." }` dicts. This helper unwraps a `s3uri` dict
 * to its underlying URI string and falls back to `JSON.stringify` for
 * other objects.
 */
export function formatScheduleParameterValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    if ("s3uri" in obj) {
      const uri = obj.s3uri;
      return typeof uri === "string" ? uri : JSON.stringify(value);
    }
    return JSON.stringify(value);
  }
  return String(value);
}

const BLANK_STRING_REQUIRED_TYPES = new Set<string>([
  "json",
  "credential_id",
  "integer",
  "float",
  "boolean",
]);

function isBlankString(value: unknown): value is string {
  return typeof value === "string" && value.trim() === "";
}

function isMissingFileUrlValue(value: unknown): boolean {
  if (isBlankString(value)) return true;
  if (typeof value !== "object" || value === null) return false;

  const fileUrlValue = value as Record<string, unknown>;

  return (
    Object.keys(fileUrlValue).length === 0 ||
    ("s3uri" in fileUrlValue &&
      (fileUrlValue.s3uri === "" || fileUrlValue.s3uri == null))
  );
}

/**
 * Mirror of `_is_missing_required_value` in
 * skyvern/forge/sdk/workflow/service.py:839. Keep these two in sync — if the
 * backend rule changes, update both.
 *
 * Rules:
 * - null/undefined is always missing
 * - string parameters allow empty strings (per UI behavior comment in backend)
 * - json / boolean / integer / float / credential_id treat empty/whitespace as missing
 * - file_url treats empty string, empty dict, or dict with empty s3uri as missing
 */
export function isMissingRequiredValue(
  parameter: WorkflowParameter,
  value: unknown,
): boolean {
  if (value == null) return true;

  const parameterType = parameter.workflow_parameter_type;

  if (parameterType === "string") {
    return false; // backend allows empty strings
  }

  if (parameterType === "file_url") {
    return isMissingFileUrlValue(value);
  }

  if (BLANK_STRING_REQUIRED_TYPES.has(parameterType)) {
    return isBlankString(value);
  }

  return false;
}

function hasStoredValue(
  storedValues: Record<string, unknown> | null | undefined,
  key: string,
): storedValues is Record<string, unknown> {
  return (
    storedValues != null &&
    Object.prototype.hasOwnProperty.call(storedValues, key)
  );
}

function normalizeDefaultValue(parameter: WorkflowParameter): unknown {
  const {
    default_value: defaultValue,
    workflow_parameter_type: parameterType,
  } = parameter;

  if (parameterType === "json") {
    return typeof defaultValue === "string"
      ? defaultValue
      : JSON.stringify(defaultValue, null, 2);
  }

  if (parameterType === "boolean") {
    // Backend stores booleans as strings; coerce to real boolean for the form.
    return defaultValue === true || defaultValue === "true";
  }

  return defaultValue;
}

function resolveInitialParameterValue(
  parameter: WorkflowParameter,
  storedValues: Record<string, unknown> | null | undefined,
): unknown {
  const {
    key,
    default_value: defaultValue,
    workflow_parameter_type: parameterType,
  } = parameter;

  if (hasStoredValue(storedValues, key)) {
    return storedValues[key];
  }

  if (defaultValue != null) {
    return normalizeDefaultValue(parameter);
  }

  return parameterType === "string" ? "" : null;
}

/**
 * Build the parameters payload to send to the backend on schedule
 * create/update. The schedule parameter form is seeded with a value for
 * every workflow parameter (default-or-blank), and we want to:
 *
 *  1. Always include REQUIRED parameters (no default_value), even when
 *     the value is an empty string. The backend's
 *     `_is_missing_required_value` allows `""` for the `string` type, so
 *     stripping empty strings here would let an empty required string
 *     pass client validation but get dropped from the payload — at which
 *     point the backend treats the key as missing and 400s.
 *  2. For OPTIONAL parameters (have a default_value), omit any value
 *     that still matches the seeded initial. Persisting an untouched
 *     default would pin it into the schedule and change semantics from
 *     "use workflow default at execution time" to "freeze current
 *     default", causing silent drift after the workflow default changes.
 *  3. Strip placeholder empties (`""`, `{}`, `{ s3uri: "" }`) for
 *     non-required parameters so the backend doesn't reject the key.
 */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Object.prototype.toString.call(value) === "[object Object]";
}

function sanitizeOptionalValue(value: unknown): unknown {
  if (value == null) return undefined;
  if (typeof value === "string") {
    return value.trim() === "" ? undefined : value;
  }
  if (Array.isArray(value)) {
    const sanitized = value
      .map((item) => sanitizeOptionalValue(item))
      .filter((item) => item !== undefined);
    return sanitized.length > 0 ? sanitized : undefined;
  }
  if (isPlainObject(value)) {
    if (isMissingFileUrlValue(value)) return undefined;
    const entries = Object.entries(value).flatMap(([key, nested]) => {
      const sanitized = sanitizeOptionalValue(nested);
      return sanitized === undefined ? [] : ([[key, sanitized]] as const);
    });
    return entries.length > 0 ? Object.fromEntries(entries) : undefined;
  }
  return value;
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a == null || b == null) return false;
  if (typeof a !== typeof b) return false;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    return a.every((item, idx) => deepEqual(item, b[idx]));
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const keysA = Object.keys(a);
    const keysB = Object.keys(b);
    if (keysA.length !== keysB.length) return false;
    return keysA.every(
      (key) =>
        Object.prototype.hasOwnProperty.call(b, key) &&
        deepEqual(a[key], b[key]),
    );
  }
  return false;
}

export function buildScheduleParametersPayload(
  values: Record<string, unknown>,
  parameters: ReadonlyArray<Parameter>,
): Record<string, unknown> | null {
  const result: Record<string, unknown> = {};

  for (const parameter of parameters) {
    if (!isScheduleParameter(parameter)) continue;

    const key = parameter.key;
    if (!Object.prototype.hasOwnProperty.call(values, key)) continue;
    const value = values[key];

    if (isRequired(parameter)) {
      // Required parameters must always round-trip — even an empty
      // string for required `string` (which the backend allows).
      result[key] =
        value == null && parameter.workflow_parameter_type === "string"
          ? ""
          : value;
      continue;
    }

    // Optional parameter: omit if it still matches the seeded default.
    const seeded = resolveInitialParameterValue(parameter, null);
    if (deepEqual(value, seeded)) continue;

    const sanitized = sanitizeOptionalValue(value);
    if (sanitized === undefined) continue;
    result[key] = sanitized;
  }

  return Object.keys(result).length > 0 ? result : null;
}

/**
 * Schedule-flavored variant of `getInitialValues` in
 * `skyvern-frontend/src/routes/workflows/utils.ts`.
 *
 * Differs from the run-form equivalent in three ways:
 * - Reads stored values from a `storedValues` record instead of router state
 * - Narrows `Parameter[]` to schedule-editable workflow parameters via `isScheduleParameter`
 * - Drops unknown keys from `storedValues` so removed workflow parameters
 *   do not leak into the submission payload
 *
 * Keep the JSON / boolean default coercion logic aligned with
 * `getInitialValues` so both helpers produce consistent form state.
 *
 * Builds the initial values record for a schedule form. Each user-facing
 * workflow parameter is seeded with:
 *   1. the value already stored on the schedule, else
 *   2. the parameter's `default_value`, else
 *   3. an empty string for `"string"` parameters, `null` otherwise.
 */
export function buildInitialParameterValues(
  parameters: ReadonlyArray<Parameter>,
  storedValues: Record<string, unknown> | null | undefined,
): Record<string, unknown> {
  const initialValues: Record<string, unknown> = {};

  for (const parameter of parameters) {
    if (!isScheduleParameter(parameter)) continue;
    initialValues[parameter.key] = resolveInitialParameterValue(
      parameter,
      storedValues,
    );
  }

  return initialValues;
}
