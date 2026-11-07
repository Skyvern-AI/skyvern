import { describe, expect, test } from "vitest";

import { isCappedValue, isTruncationMarker } from "./responseValueCaps";

const marker = {
  truncated: true,
  reason: "exceeded_max_run_response_value_size",
  original_size_bytes: 3_000_000,
  limit_bytes: 2_097_152,
};

describe("isCappedValue", () => {
  test("finds each backend cap shape", () => {
    expect(isCappedValue(marker)).toBe(true);
    expect(isCappedValue({ keep: 1, _truncated: marker })).toBe(true);
    expect(isCappedValue([{ id: 1 }, marker])).toBe(true);
  });

  test("finds a marker nested below the root", () => {
    expect(isCappedValue({ rows: [{ id: 1 }, marker], count: 2 })).toBe(true);
    expect(
      isCappedValue({ output: { pages: { first: { _truncated: marker } } } }),
    ).toBe(true);
  });

  test("leaves uncapped values alone", () => {
    expect(isCappedValue({ rows: [{ truncated: true }], count: 1 })).toBe(
      false,
    );
    expect(isCappedValue("exceeded_max_run_response_value_size")).toBe(false);
    expect(isCappedValue(null)).toBe(false);
  });
});

describe("isTruncationMarker", () => {
  test("matches only the backend marker", () => {
    expect(isTruncationMarker(marker)).toBe(true);
    expect(isTruncationMarker({ truncated: true, reason: "other" })).toBe(
      false,
    );
  });
});
