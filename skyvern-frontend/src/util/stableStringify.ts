type StableStringifyOptions = {
  omit?: (key: string) => boolean;
};

function stableStringify(
  value: unknown,
  options?: StableStringifyOptions,
): string | undefined {
  const omit = options?.omit;
  return JSON.stringify(value, (key, v) => {
    // Replacer is called once with key="" for the root; never omit the root.
    if (key !== "" && omit !== undefined && omit(key)) return undefined;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      const sorted: Record<string, unknown> = {};
      for (const k of Object.keys(v as Record<string, unknown>).sort()) {
        sorted[k] = (v as Record<string, unknown>)[k];
      }
      return sorted;
    }
    return v;
  });
}

export { stableStringify };
