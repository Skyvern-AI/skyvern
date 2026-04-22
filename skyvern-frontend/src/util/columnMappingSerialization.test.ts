import { describe, expect, it } from "vitest";
import {
  parseColumnMapping,
  serializeColumnMapping,
  resolveDestination,
  type ColumnMappingEntry,
} from "./columnMappingSerialization";

describe("parseColumnMapping", () => {
  it("returns an empty array for empty input", () => {
    expect(parseColumnMapping("")).toEqual([]);
    expect(parseColumnMapping("   ")).toEqual([]);
  });

  it("parses a JSON object of source -> letter into entries", () => {
    expect(parseColumnMapping('{"name":"A","email":"B"}')).toEqual([
      { key: "name", letter: "A" },
      { key: "email", letter: "B" },
    ]);
  });

  it("returns an empty array for malformed JSON", () => {
    expect(parseColumnMapping("{not json")).toEqual([]);
  });

  it("returns an empty array for non-object JSON", () => {
    expect(parseColumnMapping('"hello"')).toEqual([]);
    expect(parseColumnMapping("[1,2,3]")).toEqual([]);
  });

  it("coerces non-string values to strings", () => {
    expect(parseColumnMapping('{"a":1}')).toEqual([{ key: "a", letter: "1" }]);
  });
});

describe("serializeColumnMapping", () => {
  it("produces an empty string for an empty array", () => {
    expect(serializeColumnMapping([])).toBe("");
  });

  it("serializes entries to a JSON object, in order", () => {
    const entries: ColumnMappingEntry[] = [
      { key: "name", letter: "A" },
      { key: "email", letter: "B" },
    ];
    expect(serializeColumnMapping(entries)).toBe('{"name":"A","email":"B"}');
  });

  it("drops entries with empty key or empty letter", () => {
    expect(
      serializeColumnMapping([
        { key: "", letter: "A" },
        { key: "name", letter: "" },
        { key: "email", letter: "B" },
      ]),
    ).toBe('{"email":"B"}');
  });

  it("keeps only the last entry when keys collide", () => {
    expect(
      serializeColumnMapping([
        { key: "name", letter: "A" },
        { key: "name", letter: "C" },
      ]),
    ).toBe('{"name":"C"}');
  });

  it("round-trips a parsed value without change", () => {
    const json = '{"name":"A","email":"B"}';
    expect(serializeColumnMapping(parseColumnMapping(json))).toBe(json);
  });
});

describe("resolveDestination", () => {
  const headers = [
    { letter: "A", name: "Name" },
    { letter: "B", name: "Email" },
    { letter: "C", name: "Date" },
  ];

  it("returns an uppercased letter when input looks like a column letter", () => {
    expect(resolveDestination("a", headers)).toBe("A");
    expect(resolveDestination("AA", headers)).toBe("AA");
  });

  it("resolves a header name (case-insensitive) to its letter", () => {
    expect(resolveDestination("Name", headers)).toBe("A");
    expect(resolveDestination("email", headers)).toBe("B");
  });

  it("preserves user casing when no header matches and not a pure column letter", () => {
    expect(resolveDestination("  Phone Number  ", [])).toBe("Phone Number");
  });

  it("does not treat long all-caps words as column letters", () => {
    expect(resolveDestination("TOTAL", [])).toBe("TOTAL");
    expect(resolveDestination("UNKNOWN", [])).toBe("UNKNOWN");
  });

  it("returns empty string for empty input", () => {
    expect(resolveDestination("", headers)).toBe("");
    expect(resolveDestination("   ", headers)).toBe("");
  });
});
