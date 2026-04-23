import { describe, it, expect } from "vitest";
import {
  buildSpreadsheetUrl,
  columnLettersToIndex,
  extractSpreadsheetIdFromUrl,
  isTemplateExpression,
} from "./googleSheetsUrl";

describe("extractSpreadsheetIdFromUrl", () => {
  it("extracts id from a canonical /edit URL", () => {
    expect(
      extractSpreadsheetIdFromUrl(
        "https://docs.google.com/spreadsheets/d/1AbC_def-123/edit#gid=0",
      ),
    ).toBe("1AbC_def-123");
  });

  it("extracts id from a URL without trailing path", () => {
    expect(
      extractSpreadsheetIdFromUrl(
        "https://docs.google.com/spreadsheets/d/1AbC_def-123",
      ),
    ).toBe("1AbC_def-123");
  });

  it("returns the input when given a bare id", () => {
    const bare = "1AbC_def-1234567890123456789";
    expect(extractSpreadsheetIdFromUrl(bare)).toBe(bare);
  });

  it("returns null for an empty string", () => {
    expect(extractSpreadsheetIdFromUrl("")).toBeNull();
  });

  it("returns null for a non-spreadsheet URL", () => {
    expect(extractSpreadsheetIdFromUrl("https://example.com")).toBeNull();
  });

  it("returns null for a Jinja template", () => {
    expect(extractSpreadsheetIdFromUrl("{{ sheet_url }}")).toBeNull();
  });

  it("returns null for too-short bare ids", () => {
    expect(extractSpreadsheetIdFromUrl("abc")).toBeNull();
  });

  it("extracts id from a multi-account /spreadsheets/u/<n>/d/<id> URL", () => {
    expect(
      extractSpreadsheetIdFromUrl(
        "https://docs.google.com/spreadsheets/u/0/d/1AbC_def-123/edit",
      ),
    ).toBe("1AbC_def-123");
  });

  it("returns null for a published /spreadsheets/d/e/... URL", () => {
    expect(
      extractSpreadsheetIdFromUrl(
        "https://docs.google.com/spreadsheets/d/e/2PACX-1vTokenStuff/pubhtml",
      ),
    ).toBeNull();
  });
});

describe("buildSpreadsheetUrl", () => {
  it("produces the canonical edit URL", () => {
    expect(buildSpreadsheetUrl("abc123")).toBe(
      "https://docs.google.com/spreadsheets/d/abc123/edit",
    );
  });
});

describe("isTemplateExpression", () => {
  it("detects Jinja variable", () => {
    expect(isTemplateExpression("{{ x }}")).toBe(true);
  });

  it("detects Jinja statement", () => {
    expect(isTemplateExpression("{% if y %}")).toBe(true);
  });

  it("returns false for plain text", () => {
    expect(isTemplateExpression("https://docs.google.com/...")).toBe(false);
  });

  it("returns false for empty string", () => {
    expect(isTemplateExpression("")).toBe(false);
  });
});

describe("columnLettersToIndex", () => {
  it("maps A through Z", () => {
    expect(columnLettersToIndex("A")).toBe(1);
    expect(columnLettersToIndex("Z")).toBe(26);
  });

  it("maps double-letter columns past Z", () => {
    expect(columnLettersToIndex("AA")).toBe(27);
    expect(columnLettersToIndex("AZ")).toBe(52);
    expect(columnLettersToIndex("ZZ")).toBe(702);
  });

  it("maps triple-letter columns up to ZZZ", () => {
    expect(columnLettersToIndex("AAA")).toBe(703);
    expect(columnLettersToIndex("ZZZ")).toBe(18278);
  });

  it("is case-insensitive", () => {
    expect(columnLettersToIndex("aa")).toBe(27);
  });

  it("returns 0 for non-alpha input", () => {
    expect(columnLettersToIndex("")).toBe(0);
    expect(columnLettersToIndex("A1")).toBe(0);
    expect(columnLettersToIndex("1")).toBe(0);
  });

  it("returns 0 for header-name strings past 3 letters", () => {
    // Sheets columns top out at ZZZ, so longer all-caps tokens are literal
    // header names (e.g. "TOTAL"), not column references.
    expect(columnLettersToIndex("TOTAL")).toBe(0);
    expect(columnLettersToIndex("AAAA")).toBe(0);
  });
});
