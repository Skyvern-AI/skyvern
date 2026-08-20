// @vitest-environment jsdom

import type { ComponentPropsWithoutRef } from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  type BrowserSession,
  type Recording,
} from "@/routes/workflows/types/browserSessionTypes";

import { basicLocalTimeFormat } from "@/util/timeFormat";

import { BrowserSessionVideo } from "./BrowserSessionVideo";

type VideoProps = ComponentPropsWithoutRef<"video">;

vi.mock("@/components/ArtifactVideo", () => ({
  ArtifactVideo: ({ src, ...props }: VideoProps & { src: string }) => (
    <video data-testid="recording-video" src={src} {...props} />
  ),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const recording = (
  id: string,
  overrides: Partial<Recording> = {},
): Recording => ({
  artifact_id: `art_${id}`,
  checksum: `checksum-${id}`,
  file_size: 1024,
  filename: `${id}.webm`,
  modified_at: "2026-08-16T12:34:56.000Z",
  url: `https://api.skyvern.test/v1/artifacts/art_${id}/content?expiry=9999999999`,
  ...overrides,
});

const browserSession = (
  overrides: Partial<BrowserSession> = {},
): BrowserSession => ({
  browser_address: null,
  browser_session_id: "session-1",
  completed_at: "2026-08-16T12:34:56.000Z",
  downloaded_files: null,
  recordings: [],
  runnable_id: null,
  runnable_type: null,
  started_at: "2026-08-16T12:00:00.000Z",
  status: "completed",
  timeout: null,
  vnc_streaming_supported: true,
  ...overrides,
});

function videoSrc() {
  return screen.getByTestId("recording-video").getAttribute("src");
}

describe("BrowserSessionVideo", () => {
  it("renders one recording directly without duplicate tab hierarchy or a selector", () => {
    const selected = recording("one");
    render(
      <BrowserSessionVideo
        browserSession={browserSession({ recordings: [selected] })}
      />,
    );

    expect(videoSrc()).toBe(selected.url);
    expect(screen.queryByRole("combobox", { name: "Recording" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Recordings" })).toBeNull();
    expect(
      screen.getByRole("link", { name: "Download" }).getAttribute("href"),
    ).toBe(selected.url);
    expect(screen.queryByText(/Checksum:/)).toBeNull();
  });

  it("uses a selector at two recordings and changes the player and download", () => {
    const newest = recording("newest", {
      modified_at: "2026-08-16T13:00:00.000Z",
    });
    const older = recording("older", {
      modified_at: "2026-08-16T12:00:00.000Z",
    });
    render(
      <BrowserSessionVideo
        browserSession={browserSession({ recordings: [newest, older] })}
      />,
    );

    expect(videoSrc()).toBe(newest.url);
    const selector = screen.getByRole("combobox", { name: "Recording" });
    fireEvent.keyDown(selector, { key: "ArrowDown" });
    fireEvent.click(
      screen.getByRole("option", {
        name: `Recording — ${basicLocalTimeFormat(older.modified_at!)}`,
      }),
    );

    expect(videoSrc()).toBe(older.url);
    expect(
      screen.getByRole("link", { name: "Download" }).getAttribute("href"),
    ).toBe(older.url);
  });

  it("refetches a legacy storage recording before downloading", async () => {
    const stale = recording("legacy", {
      artifact_id: null,
      url: "https://bucket.s3.amazonaws.com/recording.webm?X-Amz-Signature=stale",
    });
    const fresh = {
      ...stale,
      url: "https://bucket.s3.amazonaws.com/recording.webm?X-Amz-Signature=fresh",
    };
    const refreshBrowserSession = vi
      .fn()
      .mockResolvedValue(browserSession({ recordings: [fresh] }));
    const fakeTab = { location: { href: "" }, opener: {} } as unknown as Window;
    const openSpy = vi.spyOn(window, "open").mockReturnValue(fakeTab);

    try {
      render(
        <BrowserSessionVideo
          browserSession={browserSession({ recordings: [stale] })}
          refreshBrowserSession={refreshBrowserSession}
        />,
      );

      expect(
        fireEvent.click(screen.getByRole("link", { name: "Download" })),
      ).toBe(false);
      expect(refreshBrowserSession).toHaveBeenCalledOnce();
      expect(openSpy).toHaveBeenCalledWith("", "_blank");
      expect(fakeTab.opener).toBeNull();
      await waitFor(() => {
        expect(fakeTab.location.href).toBe(fresh.url);
      });
    } finally {
      openSpy.mockRestore();
    }
  });

  it("preserves a stable selected artifact when a newer recording arrives", () => {
    const newest = recording("newest");
    const selected = recording("selected", {
      modified_at: "2026-08-16T12:00:00.000Z",
    });
    const renderResult = render(
      <BrowserSessionVideo
        browserSession={browserSession({ recordings: [newest, selected] })}
      />,
    );

    fireEvent.keyDown(screen.getByRole("combobox", { name: "Recording" }), {
      key: "ArrowDown",
    });
    fireEvent.click(
      screen.getByRole("option", {
        name: `Recording — ${basicLocalTimeFormat(selected.modified_at!)}`,
      }),
    );
    expect(videoSrc()).toBe(selected.url);

    const later = recording("later", {
      modified_at: "2026-08-16T14:00:00.000Z",
    });
    renderResult.rerender(
      <BrowserSessionVideo
        browserSession={browserSession({
          recordings: [later, newest, selected],
        })}
      />,
    );

    expect(videoSrc()).toBe(selected.url);
  });

  it("uses the current first duplicate intrinsic identity after refetch", () => {
    const duplicated = (id: string) =>
      recording(id, {
        artifact_id: null,
        checksum: "same-checksum",
        filename: "same.webm",
        modified_at: "2026-08-16T12:00:00.000Z",
      });
    const first = duplicated("first");
    const second = duplicated("second");
    const renderResult = render(
      <BrowserSessionVideo
        browserSession={browserSession({ recordings: [first, second] })}
      />,
    );

    fireEvent.keyDown(screen.getByRole("combobox", { name: "Recording" }), {
      key: "ArrowDown",
    });
    fireEvent.click(
      screen.getByRole("option", {
        name: `Recording — ${basicLocalTimeFormat(first.modified_at!)} (2)`,
      }),
    );
    expect(videoSrc()).toBe(second.url);

    const later = duplicated("later");
    renderResult.rerender(
      <BrowserSessionVideo
        browserSession={browserSession({
          recordings: [later, first, second],
        })}
      />,
    );

    expect(videoSrc()).toBe(later.url);
  });

  it("resets to the first recording after refetch when no intrinsic identity exists", () => {
    const withoutIdentity = (id: string) =>
      recording(id, {
        artifact_id: null,
        checksum: null,
        filename: null,
        modified_at: null,
      });
    const first = withoutIdentity("first");
    const second = withoutIdentity("second");
    const renderResult = render(
      <BrowserSessionVideo
        browserSession={browserSession({ recordings: [first, second] })}
      />,
    );

    fireEvent.keyDown(screen.getByRole("combobox", { name: "Recording" }), {
      key: "ArrowDown",
    });
    fireEvent.click(screen.getByRole("option", { name: "Recording 2" }));
    expect(videoSrc()).toBe(second.url);

    const later = withoutIdentity("later");
    renderResult.rerender(
      <BrowserSessionVideo
        browserSession={browserSession({ recordings: [later, first, second] })}
      />,
    );

    expect(videoSrc()).toBe(later.url);
  });

  it.each([
    [
      "created",
      "Preparing browser",
      "Waiting for the browser session to start...",
    ],
    [
      "retry",
      "Preparing browser",
      "Waiting for the browser session to start...",
    ],
    [
      "running",
      "Recording in progress",
      "Recordings will be available after the session ends.",
    ],
  ])("renders the %s session state", (status, primary, supporting) => {
    render(
      <BrowserSessionVideo
        browserSession={browserSession({ status, recordings: [] })}
      />,
    );

    expect(screen.getByText(primary)).toBeTruthy();
    expect(screen.getByText(supporting)).toBeTruthy();
    expect(screen.queryByTestId("recording-video")).toBeNull();
  });

  it("shows finalization-pending copy inside the bounded post-terminal window", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-08-16T12:00:00.000Z"));
      render(
        <BrowserSessionVideo
          browserSession={browserSession({
            completed_at: "2026-08-16T11:59:00.000Z",
            recordings: [],
          })}
        />,
      );
      expect(screen.getByText("Recordings are still processing.")).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("exits finalization-pending copy when its two-minute window elapses", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-08-16T12:00:00.000Z"));
      render(
        <BrowserSessionVideo
          browserSession={browserSession({
            completed_at: "2026-08-16T11:59:00.000Z",
            recordings: [],
          })}
        />,
      );
      expect(screen.getByText("Recordings are still processing.")).toBeTruthy();

      act(() => {
        vi.advanceTimersByTime(60_000);
      });

      expect(
        screen.getByText("No recordings were created for this session."),
      ).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows terminal empty and unavailable-video states without a stale download link", () => {
    const { rerender } = render(
      <BrowserSessionVideo
        browserSession={browserSession({ recordings: [] })}
      />,
    );
    expect(
      screen.getByText("No recordings were created for this session."),
    ).toBeTruthy();

    rerender(
      <BrowserSessionVideo
        browserSession={browserSession({
          recordings: [recording("processing", { url: "" })],
        })}
      />,
    );
    expect(
      screen.getByText("This recording is still processing."),
    ).toBeTruthy();
    expect(screen.queryByTestId("recording-video")).toBeNull();
    expect(screen.queryByRole("link", { name: "Download" })).toBeNull();
  });
});
