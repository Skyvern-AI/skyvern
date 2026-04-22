import type { GoogleSheetsWriteNode } from "./types";

const missing = (value: string): boolean => !value.trim();

export function validateGoogleSheetsWriteNode(
  node: GoogleSheetsWriteNode,
): Array<string> {
  const errors: Array<string> = [];
  const { label } = node.data;
  if (missing(node.data.credentialId)) {
    errors.push(`${label}: Google account is required.`);
  }
  if (missing(node.data.spreadsheetUrl)) {
    errors.push(`${label}: Spreadsheet is required.`);
  }
  if (missing(node.data.sheetName)) {
    errors.push(`${label}: Sheet name is required.`);
  }
  if (!node.data.values.trim()) {
    errors.push(`${label}: Values are required.`);
  }
  if (node.data.writeMode === "update" && !node.data.range.trim()) {
    errors.push(`${label}: Range is required when write mode is Update range.`);
  }
  const mapping = node.data.columnMapping?.trim();
  if (mapping) {
    try {
      const parsed = JSON.parse(mapping);
      if (
        parsed === null ||
        typeof parsed !== "object" ||
        Array.isArray(parsed)
      ) {
        errors.push(
          `${label}: Column mapping must be a JSON object of source field -> column letter.`,
        );
      }
    } catch {
      errors.push(`${label}: Column mapping is not valid JSON.`);
    }
  }
  return errors;
}
