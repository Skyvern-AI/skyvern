export type ExtraParamRow = { key: string; value: string };

// Mirrors RESERVED_EXTRA_PARAMETER_KEYS in skyvern/forge/sdk/schemas/custom_llms.py — these are
// derived from the config or passed explicitly at the invocation boundary, so the backend rejects
// them. Flag them client-side too so the user gets an inline error instead of a 400.
export const RESERVED_EXTRA_PARAM_KEYS = new Set([
  "model",
  "messages",
  "api_key",
  "api_base",
  "api_version",
  "model_info",
  "custom_llm_provider",
  "drop_params",
  "stream",
  "tools",
]);

export const MAX_EXTRA_PARAM_COUNT = 30;

export function parseExtraParamValue(value: string): unknown {
  const trimmed = value.trim();
  if (trimmed === "") {
    return "";
  }
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

export function extraParamRowValue(value: unknown): string {
  if (typeof value === "string") {
    // Show the raw string only when it survives a no-op save unchanged. Any string that
    // parseExtraParamValue would re-type (JSON scalars like "123"/"true", JSON string literals
    // like "\"foo\"", or values with trimmed whitespace) is quoted so it round-trips back verbatim.
    if (parseExtraParamValue(value) === value) {
      return value;
    }
    return JSON.stringify(value);
  }
  return JSON.stringify(value);
}

export function extraParamsToRows(
  extraParameters: Record<string, unknown> | null | undefined,
): ExtraParamRow[] {
  if (!extraParameters) {
    return [];
  }
  return Object.entries(extraParameters).map(([key, value]) => ({
    key,
    value: extraParamRowValue(value),
  }));
}

export function buildExtraParameters(rows: ExtraParamRow[]): {
  params: Record<string, unknown>;
  error: string | null;
} {
  const params: Record<string, unknown> = {};
  for (const row of rows) {
    const key = row.key.trim();
    if (!key) {
      if (row.value.trim()) {
        return { params, error: "Advanced parameter rows must have a name." };
      }
      continue;
    }
    if (RESERVED_EXTRA_PARAM_KEYS.has(key.toLowerCase())) {
      return { params, error: `"${key}" is a reserved parameter name.` };
    }
    if (key in params) {
      return { params, error: `Duplicate parameter name "${key}".` };
    }
    params[key] = parseExtraParamValue(row.value);
  }
  if (Object.keys(params).length > MAX_EXTRA_PARAM_COUNT) {
    return {
      params,
      error: `Add at most ${MAX_EXTRA_PARAM_COUNT} advanced parameters.`,
    };
  }
  return { params, error: null };
}
