import { describe, expect, test, vi } from "vitest";

import {
  artifactIdFromContentUrl,
  expiryFromSignedUrl,
  freshArtifactUrl,
  getWithMintRetry,
  refreshDelayMs,
} from "./artifactUrls";

const { axiosGetMock, clientGetMock, getClientMock } = vi.hoisted(() => {
  const clientGetMock = vi.fn();
  return {
    axiosGetMock: vi.fn(),
    clientGetMock,
    getClientMock: vi.fn().mockResolvedValue({ get: clientGetMock }),
  };
});

vi.mock("axios", () => ({
  default: { get: axiosGetMock },
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: getClientMock,
}));

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

describe("freshArtifactUrl", () => {
  test("mints a fresh URL for artifact content URLs", async () => {
    clientGetMock.mockResolvedValueOnce({
      data: { artifact_id: "a_1", signed_url: "https://fresh", expires_at: 2 },
    });
    await expect(
      freshArtifactUrl(
        null,
        "https://api.skyvern.com/v1/artifacts/a_1/content?expiry=1&kid=k&sig=s",
      ),
    ).resolves.toBe("https://fresh");
    expect(clientGetMock).toHaveBeenCalledWith("/artifacts/a_1/signed-url");
    // The signed-url route only exists on the `/v1` router (not `/api/v1`).
    expect(getClientMock).toHaveBeenCalledWith(null, "sans-api-v1");
  });

  test("returns non-artifact URLs unchanged without minting", async () => {
    clientGetMock.mockClear();
    const presigned = "https://bucket.s3.amazonaws.com/f.pdf?X-Amz-Signature=x";
    await expect(freshArtifactUrl(null, presigned)).resolves.toBe(presigned);
    expect(clientGetMock).not.toHaveBeenCalled();
  });

  test("falls back to the original URL when minting fails", async () => {
    clientGetMock.mockRejectedValueOnce(new Error("boom"));
    const url =
      "https://api.skyvern.com/v1/artifacts/a_1/content?expiry=1&kid=k&sig=s";
    await expect(freshArtifactUrl(null, url)).resolves.toBe(url);
  });
});

describe("getWithMintRetry", () => {
  test("returns the first fetch when it succeeds", async () => {
    axiosGetMock.mockResolvedValueOnce({ data: "body" });
    await expect(getWithMintRetry("https://u1", "a_1", null)).resolves.toBe(
      "body",
    );
    expect(axiosGetMock).toHaveBeenCalledTimes(1);
  });

  test("retries once on a freshly minted URL when the fetch fails", async () => {
    axiosGetMock.mockRejectedValueOnce(new Error("403"));
    clientGetMock.mockResolvedValueOnce({
      data: { artifact_id: "a_1", signed_url: "https://fresh", expires_at: 2 },
    });
    axiosGetMock.mockResolvedValueOnce({ data: "fresh-body" });

    await expect(getWithMintRetry("https://u1", "a_1", null)).resolves.toBe(
      "fresh-body",
    );
    expect(axiosGetMock).toHaveBeenLastCalledWith("https://fresh");
  });

  test("surfaces the original error when minting fails", async () => {
    const original = new Error("original 403");
    axiosGetMock.mockRejectedValueOnce(original);
    clientGetMock.mockRejectedValueOnce(new Error("mint failed"));
    await expect(getWithMintRetry("https://u1", "a_1", null)).rejects.toBe(
      original,
    );
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
