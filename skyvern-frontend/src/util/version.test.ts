import { formatVersion } from "./version";
import { describe, test, expect } from "vitest";

describe("formatVersion", () => {
  test("passes through semver tags as-is", () => {
    expect(formatVersion("1.2.3")).toBe("1.2.3");
    expect(formatVersion("v2.0.0-beta.1")).toBe("v2.0.0-beta.1");
  });

  test("passes through arbitrary strings as-is", () => {
    expect(formatVersion("custom-build")).toBe("custom-build");
  });

  test("truncates full git SHAs to 7 characters", () => {
    expect(formatVersion("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2")).toBe(
      "a1b2c3d",
    );
  });
});
