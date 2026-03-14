import { describe, it, expect } from "vitest";
import { formatTimeRemaining } from "@/util/timeFormat";

describe("formatTimeRemaining", () => {
  it("formats 0 seconds as 0:00", () => {
    expect(formatTimeRemaining(0)).toBe("0:00");
  });

  it("formats negative seconds as 0:00", () => {
    expect(formatTimeRemaining(-5)).toBe("0:00");
  });

  it("formats 65 seconds as 1:05", () => {
    expect(formatTimeRemaining(65)).toBe("1:05");
  });

  it("formats 600 seconds as 10:00", () => {
    expect(formatTimeRemaining(600)).toBe("10:00");
  });

  it("formats 59 seconds as 0:59", () => {
    expect(formatTimeRemaining(59)).toBe("0:59");
  });
});
