// @vitest-environment jsdom

import { fireEvent, render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ArtifactDownloadLink } from "./ArtifactDownloadLink";

const { freshMock } = vi.hoisted(() => ({
  freshMock: vi.fn(),
}));

vi.mock("@/api/artifactUrls", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/artifactUrls")>()),
  freshArtifactUrl: freshMock,
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const ARTIFACT_URL =
  "https://api.skyvern.com/v1/artifacts/a_dl/content?expiry=1&kid=k&sig=s";
const MINTED_URL =
  "https://api.skyvern.com/v1/artifacts/a_dl/content?expiry=2&kid=k&sig=fresh";

describe("ArtifactDownloadLink", () => {
  it("opens the tab synchronously in the click gesture, then assigns the minted URL", async () => {
    freshMock.mockResolvedValue(MINTED_URL);
    const fakeTab = { location: { href: "" }, opener: {} } as unknown as Window;
    const openSpy = vi.spyOn(window, "open").mockReturnValue(fakeTab);

    const { container } = render(
      <ArtifactDownloadLink href={ARTIFACT_URL} target="_blank">
        file.pdf
      </ArtifactDownloadLink>,
    );
    const anchor = container.querySelector("a")!;
    const notPrevented = fireEvent.click(anchor);

    expect(notPrevented).toBe(false);
    // Opened before the mint resolves, or popup blockers kill it.
    expect(openSpy).toHaveBeenCalledWith("", "_blank");
    expect(fakeTab.opener).toBeNull();
    expect(freshMock).toHaveBeenCalledWith(null, ARTIFACT_URL);
    await waitFor(() => {
      expect(fakeTab.location.href).toBe(MINTED_URL);
    });
    openSpy.mockRestore();
  });

  it("keeps native behavior for modified clicks", () => {
    freshMock.mockClear();
    const { container } = render(
      <ArtifactDownloadLink href={ARTIFACT_URL}>file.pdf</ArtifactDownloadLink>,
    );
    const anchor = container.querySelector("a")!;
    const notPrevented = fireEvent.click(anchor, { metaKey: true });

    expect(notPrevented).toBe(true);
    expect(freshMock).not.toHaveBeenCalled();
  });
});
