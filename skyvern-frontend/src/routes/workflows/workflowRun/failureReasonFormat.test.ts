import { describe, expect, test } from "vitest";

import {
  failureDetailIsLong,
  formatFailureReason,
} from "./failureReasonFormat";

describe("formatFailureReason", () => {
  test("leads with the inner cause of a nested block failure", () => {
    const raw =
      "login block failed. failure reason: Max retries per step (3) exceeded. " +
      "Possible failure reasons: … website is currently down for maintenance …";
    expect(formatFailureReason(raw)).toEqual({
      headline: "Max retries per step (3) exceeded",
      detail:
        "Max retries per step (3) exceeded. " +
        "Possible failure reasons: … website is currently down for maintenance …",
    });
  });

  test("leads with the error code of a terminated block", () => {
    expect(
      formatFailureReason(
        "navigation block terminated. Reason: DATA_UNAVAILABLE: …",
      ),
    ).toEqual({
      headline: "DATA_UNAVAILABLE: …",
      detail: "DATA_UNAVAILABLE: …",
    });
  });

  test("leads with the cause of a timed-out block", () => {
    expect(
      formatFailureReason(
        "navigation block timed out. Reason: Page did not load within 60 seconds",
      ).headline,
    ).toBe("Page did not load within 60 seconds");
  });

  test("keeps a non-block wrapper as the headline", () => {
    expect(
      formatFailureReason(
        "Setup workflow failed. failure reason: Browser session could not be created. Retry later.",
      ),
    ).toEqual({
      headline: "Setup workflow failed",
      detail: "Browser session could not be created. Retry later.",
    });
  });

  test("keeps an unsplittable cause reachable as the detail", () => {
    const cause =
      "Invalid template: unexpected end of template, expected 'end of print statement' while rendering {{ parameters.account_number | default(missing_value) }}";
    expect(
      formatFailureReason(`task block failed. failure reason: ${cause}`),
    ).toEqual({
      headline: cause,
      detail: cause,
    });
  });

  test("unescapes literal \\n sequences into real line breaks", () => {
    const raw =
      "task block failed. failure reason: Timeout 30000ms exceeded.\\nCall log:\\n - waiting";
    expect(formatFailureReason(raw)).toEqual({
      headline: "Timeout 30000ms exceeded",
      detail: "Timeout 30000ms exceeded.\nCall log:\n - waiting",
    });
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
