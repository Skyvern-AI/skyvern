export const SECRET_HEADER_MASK = "***";

export function isMaskedHeaders(
  headers: Record<string, unknown> | null | undefined,
): boolean {
  if (!headers) {
    return false;
  }
  return Object.values(headers).some((value) => value === SECRET_HEADER_MASK);
}

export function parseHeaderJson(value: string): Record<string, string> {
  const parsed = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Headers must be a JSON object");
  }

  const headers: Record<string, string> = {};
  for (const [key, headerValue] of Object.entries(parsed)) {
    if (key && typeof key === "string") {
      headers[key] = String(headerValue);
    }
  }
  return headers;
}
