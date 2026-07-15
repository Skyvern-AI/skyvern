import { describe, expect, test } from "vitest";

import {
  artifactIdFromContentUrl,
  expiryFromSignedUrl,
  refreshDelayMs,
} from "./artifactUrls";

describe("artifactIdFromContentUrl", () => {
  test("extracts the artifact id from a signed content URL", () => {
    expect(
      artifactIdFromContentUrl(
        "https://api.skyvern.com/v1/artifacts/a_123/content?expiry=1800000600&kid=k1&sig=s",
      ),
    ).toBe("a_123");
  });

  test("handles trailing slash and api-prefixed paths", () => {
    expect(
      artifactIdFromContentUrl(
        "https://api.skyvern.com/api/v1/artifacts/a_456/content/?expiry=1&kid=k&sig=s",
      ),
    ).toBe("a_456");
  });

  test("returns null for storage presigned URLs", () => {
    expect(
      artifactIdFromContentUrl(
        "https://bucket.s3.amazonaws.com/recordings/foo.webm?X-Amz-Signature=abc",
      ),
    ).toBeNull();
  });

  test("returns null for null, empty, and unparseable input", () => {
    expect(artifactIdFromContentUrl(null)).toBeNull();
    expect(artifactIdFromContentUrl("")).toBeNull();
    expect(artifactIdFromContentUrl("not a url")).toBeNull();
  });
});

describe("expiryFromSignedUrl", () => {
  test("reads the expiry query param as unix seconds", () => {
    expect(
      expiryFromSignedUrl(
        "https://api.skyvern.com/v1/artifacts/a_1/content?expiry=1800000600&kid=k1&sig=s",
      ),
    ).toBe(1800000600);
  });

  test("returns null when expiry is absent or not a number", () => {
    expect(
      expiryFromSignedUrl("https://api.skyvern.com/v1/artifacts/a_1/content"),
    ).toBeNull();
    expect(
      expiryFromSignedUrl(
        "https://api.skyvern.com/v1/artifacts/a_1/content?expiry=soon",
      ),
    ).toBeNull();
    expect(expiryFromSignedUrl("not a url")).toBeNull();
  });
});

describe("refreshDelayMs", () => {
  test("schedules the refresh margin before expiry", () => {
    // expires 300s from now, margin 60s -> refresh in 240s
    expect(refreshDelayMs(1_000_300, 1_000_000_000)).toBe(240_000);
  });

  test("clamps to zero when the URL is already inside the margin", () => {
    expect(refreshDelayMs(1_000_030, 1_000_000_000)).toBe(0);
    expect(refreshDelayMs(999_000, 1_000_000_000)).toBe(0);
  });
});
