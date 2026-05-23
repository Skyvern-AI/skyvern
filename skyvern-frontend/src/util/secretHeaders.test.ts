import { describe, expect, it } from "vitest";

import { parseHeaderJson } from "./secretHeaders";

describe("parseHeaderJson", () => {
  it("parses object headers and stringifies values", () => {
    expect(parseHeaderJson('{"x-api-key":"secret","x-count":3}')).toEqual({
      "x-api-key": "secret",
      "x-count": "3",
    });
  });

  it("rejects array headers", () => {
    expect(() => parseHeaderJson('["x-api-key"]')).toThrow(
      "Headers must be a JSON object",
    );
  });

  it("rejects null headers", () => {
    expect(() => parseHeaderJson("null")).toThrow(
      "Headers must be a JSON object",
    );
  });
});
