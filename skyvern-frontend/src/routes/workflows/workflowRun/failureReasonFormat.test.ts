import { describe, expect, test } from "vitest";

import {
  failureDetailIsLong,
  formatFailureReason,
} from "./failureReasonFormat";

describe("formatFailureReason", () => {
  test("splits the block headline from the nested failure-reason payload", () => {
    const raw =
      "for_loop block failed. failure reason: Failed to execute code block. " +
      "Reason: Exception: select_payer failed for 'Demo Payer' target='DEMO PAYER'";
    expect(formatFailureReason(raw)).toEqual({
      headline: "for_loop block failed",
      detail:
        "Failed to execute code block. " +
        "Reason: Exception: select_payer failed for 'Demo Payer' target='DEMO PAYER'",
    });
  });

  test("unescapes literal \\n sequences into real line breaks", () => {
    const raw =
      "task block failed. failure reason: Timeout 30000ms exceeded.\\nCall log:\\n - waiting";
    const { detail } = formatFailureReason(raw);
    expect(detail).toContain("exceeded.\nCall log:\n - waiting");
    expect(detail).not.toContain("\\n");
  });

  test("falls back to a first-sentence headline for generic prose", () => {
    const raw =
      "Login page rejected the credentials. The site returned a 403 after the second attempt and locked the account form.";
    expect(formatFailureReason(raw)).toEqual({
      headline: "Login page rejected the credentials",
      detail:
        "The site returned a 403 after the second attempt and locked the account form.",
    });
  });

  test("keeps a short single-sentence reason as the headline alone", () => {
    expect(formatFailureReason("Login page rejected the credentials")).toEqual({
      headline: "Login page rejected the credentials",
      detail: null,
    });
  });

  test("does not split on periods inside URLs or decimals", () => {
    const raw = "Navigation to https://example.com/checkout timed out";
    expect(formatFailureReason(raw).headline).toBe(raw);
  });
});

describe("failureDetailIsLong", () => {
  test("flags payloads the three-line clamp would cut", () => {
    expect(failureDetailIsLong("x".repeat(221))).toBe(true);
    expect(failureDetailIsLong("a\nb\nc\nd")).toBe(true);
    expect(failureDetailIsLong("short detail")).toBe(false);
  });
});
