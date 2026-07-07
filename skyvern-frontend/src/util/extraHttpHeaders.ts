function hasExtraHttpHeaders(
  headers: Record<string, unknown> | null | undefined,
): headers is Record<string, unknown> {
  return (
    headers !== null &&
    headers !== undefined &&
    typeof headers === "object" &&
    !Array.isArray(headers) &&
    Object.keys(headers).length > 0
  );
}

export { hasExtraHttpHeaders };
