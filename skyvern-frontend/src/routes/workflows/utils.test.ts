import { describe, expect, it } from "vitest";

import {
  normalizeJsonParameterFormValue,
  parseJsonWorkflowParameterValue,
  validateJsonWorkflowParameterValue,
} from "./utils";

describe("parseJsonWorkflowParameterValue", () => {
  it("parses a JSON array string", () => {
    expect(parseJsonWorkflowParameterValue('["1002763917"]')).toEqual([
      "1002763917",
    ]);
  });

  it("returns a single-item array unchanged (SKY-10854)", () => {
    const value = ["1002763917"];
    expect(parseJsonWorkflowParameterValue(value)).toBe(value);
    expect(parseJsonWorkflowParameterValue(value)).toEqual(["1002763917"]);
  });

  it("returns multi-item arrays unchanged", () => {
    const value = ["a", "b"];
    expect(parseJsonWorkflowParameterValue(value)).toBe(value);
  });

  it("returns parsed objects unchanged", () => {
    const value = { ids: ["1002763917"] };
    expect(parseJsonWorkflowParameterValue(value)).toBe(value);
  });
});

describe("normalizeJsonParameterFormValue", () => {
  it("stringifies parsed arrays for form state", () => {
    expect(normalizeJsonParameterFormValue(["1002763917"])).toBe(
      '[\n  "1002763917"\n]',
    );
  });

  it("leaves strings unchanged", () => {
    expect(normalizeJsonParameterFormValue('["1002763917"]')).toBe(
      '["1002763917"]',
    );
  });

  it("keeps null as null for unset JSON params", () => {
    expect(normalizeJsonParameterFormValue(null)).toBeNull();
    expect(normalizeJsonParameterFormValue(undefined)).toBeNull();
  });
});

describe("validateJsonWorkflowParameterValue", () => {
  it("accepts null as valid JSON", () => {
    expect(validateJsonWorkflowParameterValue(null)).toBe(true);
    expect(validateJsonWorkflowParameterValue(undefined)).toBe(true);
  });

  it("accepts the null JSON literal string", () => {
    expect(validateJsonWorkflowParameterValue("null")).toBe(true);
  });

  it("accepts parsed arrays from re-run state", () => {
    expect(validateJsonWorkflowParameterValue(["1002763917"])).toBe(true);
  });

  it("rejects empty input", () => {
    expect(validateJsonWorkflowParameterValue("")).toBe(
      "This field is required",
    );
    expect(validateJsonWorkflowParameterValue("   ")).toBe(
      "This field is required",
    );
  });

  it("rejects invalid JSON", () => {
    expect(validateJsonWorkflowParameterValue("{not json")).toBe(
      "Invalid JSON",
    );
  });
});
