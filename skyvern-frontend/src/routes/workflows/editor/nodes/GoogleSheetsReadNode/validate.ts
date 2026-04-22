import type { GoogleSheetsReadNode } from "./types";

const missing = (value: string): boolean => !value.trim();

export function validateGoogleSheetsReadNode(
  node: GoogleSheetsReadNode,
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
  return errors;
}
