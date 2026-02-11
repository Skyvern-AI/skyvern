import { deepEqualStringArrays } from "./equality";
import { describe, test, expect } from "vitest";

describe("deepEqualStringArrays", () => {
  test("both undefined", () => {
    expect(deepEqualStringArrays(undefined, undefined)).toBe(true);
  });

  test("one undefined, one defined", () => {
    expect(deepEqualStringArrays(undefined, ["a"])).toBe(false);
    expect(deepEqualStringArrays(["a"], undefined)).toBe(false);
  });

  test("both null", () => {
    expect(deepEqualStringArrays(null, null)).toBe(true);
  });

  test("one null, one defined", () => {
    expect(deepEqualStringArrays(null, ["a"])).toBe(false);
    expect(deepEqualStringArrays(["a"], null)).toBe(false);
  });

  test("different lengths", () => {
    expect(deepEqualStringArrays(["a"], ["a", "b"])).toBe(false);
    expect(deepEqualStringArrays(["a", "b"], ["a"])).toBe(false);
  });

  test("same elements, same order", () => {
    expect(deepEqualStringArrays(["a", "b", "c"], ["a", "b", "c"])).toBe(true);
  });

  test("same elements, different order", () => {
    expect(deepEqualStringArrays(["a", "b", "c"], ["c", "b", "a"])).toBe(false);
  });

  test("different elements", () => {
    expect(deepEqualStringArrays(["a", "b", "c"], ["a", "b", "d"])).toBe(false);
  });
});
