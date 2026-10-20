import { describe, expect, it } from "vitest";
import { fitSteps } from "./fitSteps";

const byLength = (text: string) => text.length;
const steps = ["Opened a page", "waited", "read ×10", "clicked '#next'"];

describe("fitSteps", () => {
  it("shows every step when they fit", () => {
    expect(fitSteps(steps, 1000, byLength)).toEqual([
      "Opened a page → waited → read ×10 → clicked '#next'",
      "",
    ]);
  });

  it("fills after the first step and bridges to the last", () => {
    expect(fitSteps(steps, 45, byLength)).toEqual([
      "Opened a page → waited",
      " → … → clicked '#next'",
    ]);
  });

  it("keeps the last step apart from the head when nothing else fits", () => {
    expect(fitSteps(steps, 5, byLength)).toEqual([
      "Opened a page",
      " → … → clicked '#next'",
    ]);
    expect(fitSteps(steps.slice(0, 2), 5, byLength)).toEqual([
      "Opened a page",
      " → waited",
    ]);
  });
});
