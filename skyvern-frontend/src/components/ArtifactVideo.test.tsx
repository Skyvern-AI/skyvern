// @vitest-environment jsdom

import { fireEvent, render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ArtifactVideo } from "./ArtifactVideo";

const { mintMock } = vi.hoisted(() => ({
  mintMock: vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({}),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

vi.mock("@/api/artifactUrls", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/artifactUrls")>()),
  mintSignedArtifactUrl: mintMock,
}));

const FAR_EXPIRY = Math.floor(Date.now() / 1000) + 12 * 3600;
const SIGNED_URL = `https://api.skyvern.com/v1/artifacts/a_rec/content?expiry=${FAR_EXPIRY}&kid=k1&sig=s`;
const MINTED_URL =
  "https://api.skyvern.com/v1/artifacts/a_rec/content?expiry=9999999999&kid=k1&sig=fresh";

describe("ArtifactVideo", () => {
  it("re-mints the URL once when playback errors", async () => {
    mintMock.mockResolvedValue({
      artifact_id: "a_rec",
      signed_url: MINTED_URL,
      expires_at: 9999999999,
    });

    const { container } = render(<ArtifactVideo src={SIGNED_URL} />);
    const video = container.querySelector("video")!;
    expect(video.getAttribute("src")).toBe(SIGNED_URL);

    fireEvent.error(video);

    await waitFor(() => {
      expect(video.getAttribute("src")).toBe(MINTED_URL);
    });
    expect(mintMock).toHaveBeenCalledWith(null, "a_rec");

    fireEvent.error(video);
    expect(mintMock).toHaveBeenCalledTimes(1);
  });

  it("does not try to re-mint non-artifact URLs", async () => {
    mintMock.mockClear();
    const presigned =
      "https://bucket.s3.amazonaws.com/rec.webm?X-Amz-Signature=abc";
    const { container } = render(<ArtifactVideo src={presigned} />);
    const video = container.querySelector("video")!;

    fireEvent.error(video);

    expect(mintMock).not.toHaveBeenCalled();
    expect(video.getAttribute("src")).toBe(presigned);
  });
});
