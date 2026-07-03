import { describe, expect, it } from "vitest";

import {
  formatDuration,
  normalizeJsonParameterFormValue,
  parseJsonWorkflowParameterValue,
  toDuration,
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

describe("toDuration", () => {
  it("converts sub-minute durations", () => {
    expect(toDuration(45)).toEqual({ hour: 0, minute: 0, second: 45 });
  });

  it("converts minutes and seconds", () => {
    expect(toDuration(125)).toEqual({ hour: 0, minute: 2, second: 5 });
  });

  it("converts hours, minutes, and seconds", () => {
    expect(toDuration(3661)).toEqual({ hour: 1, minute: 1, second: 1 });
  });

  it("floors fractional seconds", () => {
    expect(toDuration(90.7)).toEqual({ hour: 0, minute: 1, second: 30 });
  });

  it("preserves hours beyond 24 instead of wrapping", () => {
    // 25h exactly — `hours % 24` used to report this as 1h since Duration
    // has no day field to carry the overflow.
    expect(toDuration(90000)).toEqual({ hour: 25, minute: 0, second: 0 });
  });

  it("preserves multi-day durations in the hour field", () => {
    // 49h 1m 5s
    expect(toDuration(176465)).toEqual({ hour: 49, minute: 1, second: 5 });
  });
});

describe("formatDuration", () => {
  it("omits empty leading units", () => {
    expect(formatDuration(toDuration(45))).toBe("45s");
    expect(formatDuration(toDuration(125))).toBe("2m 5s");
    expect(formatDuration(toDuration(3661))).toBe("1h 1m 1s");
  });

  it("renders durations longer than a day without losing hours", () => {
    expect(formatDuration(toDuration(90000))).toBe("25h 0m 0s");
  });
});
