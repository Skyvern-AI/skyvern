import { describe, expect, it } from "vitest";
import {
  formatDestinationLabel,
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

  it("resolves a combined destination label (case-insensitive) to its letter", () => {
    expect(resolveDestination("A - Name", headers)).toBe("A");
    expect(resolveDestination("a - name", headers)).toBe("A");
  });

  it("resolves a combined label whose header name contains a separator", () => {
    const headersWithSeparator = [{ letter: "D", name: "Score - Notes" }];
    expect(resolveDestination("D - Score - Notes", headersWithSeparator)).toBe(
      "D",
    );
  });

  it("preserves an unmatched combined-looking string", () => {
    expect(resolveDestination("ZZ - Unknown", headers)).toBe("ZZ - Unknown");
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

describe("formatDestinationLabel", () => {
  it("formats named and unnamed headers", () => {
    expect(formatDestinationLabel({ letter: "A", name: "Name" })).toBe(
      "A - Name",
    );
    expect(formatDestinationLabel({ letter: "A", name: "  " })).toBe("A");
    expect(formatDestinationLabel({ letter: "A", name: " Padded " })).toBe(
      "A - Padded",
    );
  });
});
