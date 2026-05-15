export const SECRET_HEADER_MASK = "***";

export function isMaskedHeaders(
  headers: Record<string, unknown> | null | undefined,
): boolean {
  if (!headers) {
    return false;
  }
  return Object.values(headers).some((value) => value === SECRET_HEADER_MASK);
}
