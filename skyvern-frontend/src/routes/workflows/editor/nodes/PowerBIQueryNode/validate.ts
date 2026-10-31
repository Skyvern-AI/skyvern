import { POWERBI_QUERY_MAX_ROWS, type PowerBIQueryNode } from "./types";

const missing = (value: string): boolean => !value.trim();

export function validatePowerBIQueryNode(
  node: PowerBIQueryNode,
): Array<string> {
  const errors: Array<string> = [];
  const { label, connectionId, datasetId, query, maxRows } = node.data;

  if (missing(connectionId)) {
    errors.push(`${label}: Microsoft Power BI connection is required.`);
  } else if (connectionId.includes("{{") || connectionId.includes("{%")) {
    errors.push(
      `${label}: Microsoft Power BI connection must be a literal credential ID.`,
    );
  }
  if (missing(datasetId)) {
    errors.push(`${label}: Dataset ID is required.`);
  }
  if (missing(query)) {
    errors.push(`${label}: DAX query is required.`);
  }
  if (
    !Number.isInteger(maxRows) ||
    maxRows < 1 ||
    maxRows > POWERBI_QUERY_MAX_ROWS
  ) {
    errors.push(
      `${label}: Maximum rows must be an integer from 1 to ${POWERBI_QUERY_MAX_ROWS}.`,
    );
  }

  return errors;
}
