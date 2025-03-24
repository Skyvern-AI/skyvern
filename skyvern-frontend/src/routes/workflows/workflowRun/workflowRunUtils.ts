export function getAggregatedExtractedInformation(
  outputs: Record<string, unknown>,
) {
  const extractedInformation: Record<string, unknown> = {};
  Object.entries(outputs).forEach(([id, output]) => {
    if (
      typeof output === "object" &&
      output !== null &&
      "extracted_information" in output
    ) {
      extractedInformation[id] = output.extracted_information;
    }
  });
  return extractedInformation;
}
