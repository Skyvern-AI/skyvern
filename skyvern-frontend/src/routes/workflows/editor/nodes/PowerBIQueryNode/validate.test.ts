import { describe, expect, it } from "vitest";

import {
  POWERBI_QUERY_DEFAULT_MAX_ROWS,
  POWERBI_QUERY_MAX_ROWS,
  powerBIQueryNodeDefaultData,
  type PowerBIQueryNode,
} from "./types";
import { validatePowerBIQueryNode } from "./validate";

function queryNode(
  overrides: Partial<PowerBIQueryNode["data"]> = {},
): PowerBIQueryNode {
  return {
    id: "powerbi-query-1",
    type: "powerBIQuery",
    position: { x: 0, y: 0 },
    data: {
      ...powerBIQueryNodeDefaultData,
      label: "Query sales",
      connectionId: "moac_powerbi",
      datasetId: "dataset-1",
      query: "EVALUATE 'Sales'",
      ...overrides,
    },
  };
}

describe("Power BI query block validation", () => {
  it("defaults to 1000 rows and requires connection, dataset, and query", () => {
    expect(powerBIQueryNodeDefaultData.maxRows).toBe(
      POWERBI_QUERY_DEFAULT_MAX_ROWS,
    );
    expect(
      validatePowerBIQueryNode(
        queryNode({ connectionId: " ", datasetId: "", query: "\n" }),
      ),
    ).toEqual([
      "Query sales: Microsoft Power BI connection is required.",
      "Query sales: Dataset ID is required.",
      "Query sales: DAX query is required.",
    ]);
  });

  it.each([0, -1, 1.5, POWERBI_QUERY_MAX_ROWS + 1, Number.NaN])(
    "rejects invalid max_rows %s without coercion",
    (maxRows) => {
      expect(validatePowerBIQueryNode(queryNode({ maxRows }))).toContain(
        `Query sales: Maximum rows must be an integer from 1 to ${POWERBI_QUERY_MAX_ROWS}.`,
      );
    },
  );

  it.each(["{{ connection_id }}", "{% set connection_id = value %}"])(
    "rejects templated connection ID %s",
    (connectionId) => {
      expect(validatePowerBIQueryNode(queryNode({ connectionId }))).toContain(
        "Query sales: Microsoft Power BI connection must be a literal credential ID.",
      );
    },
  );

  it.each([1, POWERBI_QUERY_DEFAULT_MAX_ROWS, POWERBI_QUERY_MAX_ROWS])(
    "accepts the supported row cap %s",
    (maxRows) => {
      expect(validatePowerBIQueryNode(queryNode({ maxRows }))).toEqual([]);
    },
  );
});
