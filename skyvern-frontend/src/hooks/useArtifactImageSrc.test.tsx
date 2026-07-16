// @vitest-environment jsdom

import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ArtifactApiResponse } from "@/api/types";
import { useArtifactImageSrc } from "./useArtifactImageSrc";

const { mintMock } = vi.hoisted(() => ({
  mintMock: vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({}),
}));

vi.mock("@/api/artifactUrls", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/artifactUrls")>()),
  mintSignedArtifactUrl: mintMock,
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const SIGNED_URL =
  "https://api.skyvern.com/v1/artifacts/a_img/content?expiry=1&kid=k&sig=s";
const MINTED_URL =
  "https://api.skyvern.com/v1/artifacts/a_img/content?expiry=2&kid=k&sig=fresh";

const artifact = {
  artifact_id: "a_img",
  artifact_type: "screenshot_action",
  uri: "s3://bucket/a_img.png",
  signed_url: SIGNED_URL,
} as ArtifactApiResponse;

function Probe({
  probeArtifact = artifact,
}: {
  probeArtifact?: ArtifactApiResponse;
}) {
  const { src, onImageError, imageFailed } = useArtifactImageSrc(probeArtifact);
  if (imageFailed) {
    return <div data-testid="failed">failed</div>;
  }
  return <img src={src} onError={onImageError} alt="probe" />;
}

describe("useArtifactImageSrc", () => {
  it("re-mints once on image error, then fails permanently", async () => {
    mintMock.mockResolvedValue({
      artifact_id: "a_img",
      signed_url: MINTED_URL,
      expires_at: 2,
    });

    const { container, queryByTestId } = render(<Probe />);
    const img = () => container.querySelector("img")!;
    expect(img().getAttribute("src")).toBe(SIGNED_URL);

    fireEvent.error(img());
    await waitFor(() => {
      expect(img().getAttribute("src")).toBe(MINTED_URL);
    });
    expect(mintMock).toHaveBeenCalledWith(null, "a_img");
    expect(queryByTestId("failed")).toBeNull();

    fireEvent.error(img());
    await waitFor(() => {
      expect(queryByTestId("failed")).not.toBeNull();
    });
    expect(mintMock).toHaveBeenCalledTimes(1);
  });

  it("ignores a mint that resolves after the artifact changed", async () => {
    let resolveMint!: (value: unknown) => void;
    mintMock.mockReset();
    mintMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveMint = resolve;
      }),
    );

    const otherArtifact = {
      ...artifact,
      artifact_id: "a_other",
      signed_url:
        "https://api.skyvern.com/v1/artifacts/a_other/content?expiry=1&kid=k&sig=s",
    } as ArtifactApiResponse;

    const { container, rerender } = render(<Probe />);
    const img = () => container.querySelector("img")!;
    fireEvent.error(img());

    rerender(<Probe probeArtifact={otherArtifact} />);
    await act(async () => {
      resolveMint({
        artifact_id: "a_img",
        signed_url: MINTED_URL,
        expires_at: 2,
      });
    });

    expect(img().getAttribute("src")).toBe(otherArtifact.signed_url);
  });
});
