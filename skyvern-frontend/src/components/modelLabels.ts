const SKYVERN_OPTIMIZED_LABEL = "Skyvern Optimized";

const deprecatedModelNames = new Set<string>([
  "gemini-3-pro-preview",
  "gemini-3.0-flash",
  "gemini-3.5-flash",
  "azure/gpt-4.1",
  "azure/gpt-5",
  "azure/gpt-5-mini",
  "azure/gpt-5.2",
  "azure/gpt-5.4",
  "azure/gpt-5.6-sol",
  "azure/gpt-5.6-terra",
  "azure/gpt-5.6-luna",
  "azure/o3",
  "claude-haiku-4-5-20251001",
  "claude-opus-4-5-20251101",
  "claude-sonnet-4-5-20250929",
  "claude-opus-4-6",
  "claude-opus-5",
  "claude-fable-5",
  "mercury-2",
]);

function withDeprecationSuffix(modelName: string, label: string): string {
  return deprecatedModelNames.has(modelName) ? `${label} (deprecated)` : label;
}

// The label the model selector shows for a stored model. Without a catalog (still loading, or the
// query disabled in a read-only view) the selector shows the stored name, so this does too.
function getModelDisplayLabel(
  rawModels: Record<string, string> | undefined,
  modelName: string | null | undefined,
): string {
  if (modelName && !rawModels) return modelName;
  const label = modelName ? rawModels?.[modelName] : undefined;
  return modelName && label !== undefined
    ? withDeprecationSuffix(modelName, label)
    : SKYVERN_OPTIMIZED_LABEL;
}

export {
  deprecatedModelNames,
  getModelDisplayLabel,
  SKYVERN_OPTIMIZED_LABEL,
  withDeprecationSuffix,
};
