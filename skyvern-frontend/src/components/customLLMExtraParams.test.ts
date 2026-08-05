import { describe, expect, it } from "vitest";

import {
  buildExtraParameters,
  extraParamRowValue,
  extraParamsToRows,
  parseExtraParamValue,
} from "./customLLMExtraParams";

describe("extra parameter row round-tripping", () => {
  // A no-op edit takes the stored value through extraParamRowValue (display) and back through
  // parseExtraParamValue (save). The stored value must come back unchanged.
  const roundTrip = (value: unknown) =>
    parseExtraParamValue(extraParamRowValue(value));

  it.each([
    ["plain string", "flex"],
    ["JSON scalar string", "123"],
    ["boolean-like string", "true"],
    ["null-like string", "null"],
    ["JSON string literal", '"foo"'],
    ["string with padding", "  spaced  "],
    ["empty string", ""],
    ["number", 42],
    ["boolean", true],
    ["object", { type: "enabled", budget_tokens: 1024 }],
    ["nested headers", { Authorization: "Bearer x", "X-API-Key": "k" }],
  ])("preserves a %s on a no-op save", (_label, value) => {
    expect(roundTrip(value)).toStrictEqual(value);
  });

  it("does not silently retype a stored JSON string literal", () => {
    // Regression for the '"foo"' case: shown quoted so it re-parses back to the same string.
    expect(extraParamRowValue('"foo"')).toBe('"\\"foo\\""');
    expect(parseExtraParamValue('"\\"foo\\""')).toBe('"foo"');
  });
});

describe("extraParamsToRows / buildExtraParameters", () => {
  it("survives a full config round-trip", () => {
    const extraParameters = {
      service_tier: "flex",
      literal: '"foo"',
      count: "123",
      thinking: { type: "enabled", budget_tokens: 1024 },
    };
    const { params, error } = buildExtraParameters(
      extraParamsToRows(extraParameters),
    );
    expect(error).toBeNull();
    expect(params).toStrictEqual(extraParameters);
  });

  it("rejects reserved keys advertised for passthrough", () => {
    const { error } = buildExtraParameters([{ key: "tools", value: "[]" }]);
    expect(error).toContain("reserved");
  });
});
