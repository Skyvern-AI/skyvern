import { describe, test, expect } from "vitest";
import { validateErrorCodeMapping } from "./validateErrorCodeMapping";

describe("validateErrorCodeMapping", () => {
  test("clean keys return no errors", () => {
    const json = JSON.stringify({
      ACCOUNT_GROUP_NOT_FOUND: "if there are no results, terminate",
      EMPTY_TABLE: "stop",
    });
    expect(validateErrorCodeMapping("block_1", json)).toEqual([]);
  });

  test("invalid JSON returns a parse error", () => {
    const result = validateErrorCodeMapping("block_1", "{not json");
    expect(result).toHaveLength(1);
    expect(result[0]).toContain("Error messages is not valid JSON");
  });

  test("null value (mapping disabled) returns no errors", () => {
    expect(validateErrorCodeMapping("block_1", "null")).toEqual([]);
  });

  test("leading whitespace in key is flagged", () => {
    const json = JSON.stringify({ " ACCOUNT_GROUP_NOT_FOUND": "terminate" });
    const result = validateErrorCodeMapping("block_42", json);
    expect(result).toHaveLength(1);
    expect(result[0]).toContain("block_42");
    expect(result[0]).toContain("surrounding whitespace");
    expect(result[0]).toContain(" ACCOUNT_GROUP_NOT_FOUND");
  });

  test("trailing whitespace in key is flagged", () => {
    const json = JSON.stringify({ "ACCOUNT_GROUP_NOT_FOUND ": "terminate" });
    const result = validateErrorCodeMapping("block_42", json);
    expect(result).toHaveLength(1);
    expect(result[0]).toContain("surrounding whitespace");
  });

  test("tab character in key is flagged", () => {
    const json = JSON.stringify({ "\tFOO": "bar" });
    expect(validateErrorCodeMapping("block_1", json)).toHaveLength(1);
  });

  test("multiple bad keys produce multiple errors", () => {
    const json = JSON.stringify({
      " FOO": "a",
      "BAR ": "b",
      BAZ: "c",
    });
    const result = validateErrorCodeMapping("block_1", json);
    expect(result).toHaveLength(2);
  });

  test("empty object returns no errors", () => {
    expect(validateErrorCodeMapping("block_1", "{}")).toEqual([]);
  });

  test("array input is flagged as wrong shape", () => {
    // error_code_mapping must be a JSON object, not an array. Arrays are
    // syntactically valid JSON so the parse guard does not catch them —
    // the dedicated shape check does, preventing bad data from reaching
    // the save-time flow.
    const result = validateErrorCodeMapping("block_1", "[]");
    expect(result).toHaveLength(1);
    expect(result[0]).toContain("must be a JSON object");
    expect(result[0]).toContain("array");
  });

  test("JSON primitives are flagged as wrong shape", () => {
    expect(validateErrorCodeMapping("block_1", "42")[0]).toContain(
      "must be a JSON object",
    );
    expect(validateErrorCodeMapping("block_1", '"foo"')[0]).toContain(
      "must be a JSON object",
    );
    expect(validateErrorCodeMapping("block_1", "true")[0]).toContain(
      "must be a JSON object",
    );
  });
});
