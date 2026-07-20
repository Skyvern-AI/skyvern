import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { getClient } from "@/api/AxiosClient";

import { BrowserSessionTimeline } from "./BrowserSessionTimeline";
import {
  buildSessionTimeline,
  getSessionTimelineKindLabel,
} from "./BrowserSessionTimeline.utils";

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

afterEach(() => vi.clearAllMocks());

const artifact = (
  checksum: string,
  filename: string,
  modified_at: string | null,
) => ({
  checksum,
  filename,
  modified_at,
  url: `https://example.test/${filename}`,
});

function renderTimeline(browserSession: unknown, duplicate = false) {
  const get = vi.fn().mockResolvedValue({ data: browserSession });
  vi.mocked(getClient).mockResolvedValue({ get } as never);
  const timeline = <BrowserSessionTimeline />;

  render(
    <MemoryRouter initialEntries={["/browser-session/session-1/timeline"]}>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <Routes>
          <Route
            path="/browser-session/:browserSessionId/timeline"
            element={
              duplicate ? (
                <>
                  {timeline}
                  {timeline}
                </>
              ) : (
                timeline
              )
            }
          />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );

  return get;
}

describe("buildSessionTimeline", () => {
  it("sorts by time, kind, and filename while preserving exact ties", () => {
    const sameTime = "2026-07-16T11:00:00.000Z";
    const timeline = buildSessionTimeline({
      status: "completed",
      downloadedFiles: [
        artifact("zeta", "zeta.txt", sameTime),
        artifact("alpha", "alpha.txt", sameTime),
        artifact("first-tie", "same.txt", sameTime),
        artifact("second-tie", "same.txt", sameTime),
        artifact("newest", "newest.txt", "2026-07-16T12:00:00.000Z"),
        artifact("no-time", "no-time.txt", null),
      ],
      recordings: [artifact("recording", "alpha.webm", sameTime)],
    });

    expect(timeline.map(({ checksum }) => checksum)).toEqual([
      "newest",
      "alpha",
      "first-tie",
      "second-tie",
      "zeta",
      "recording",
      "no-time",
    ]);
  });

  it("handles empty inputs, nullable URLs, filenames, and unknown kinds", () => {
    expect(
      buildSessionTimeline({
        status: "completed",
        downloadedFiles: null,
        recordings: null,
      }),
    ).toEqual([]);

    const timeline = buildSessionTimeline({
      status: "completed",
      downloadedFiles: [
        { checksum: "no-url", filename: null, modified_at: null, url: null },
        {
          checksum: "url-name",
          filename: null,
          modified_at: null,
          url: "https://example.test/files/report.csv?expiry=1",
        },
      ],
      recordings: [],
    });
    expect(timeline[0]).toEqual(
      expect.objectContaining({ filename: "Download 1", url: null }),
    );
    expect(timeline[1]?.filename).toBe("report.csv");
    expect(getSessionTimelineKindLabel("future-kind")).toBe("Artifact");
  });

  it("orders mixed timezone-less and offset-carrying timestamps as UTC", () => {
    const timeline = buildSessionTimeline({
      status: "completed",
      downloadedFiles: [
        artifact("naive-oldest", "oldest.txt", "2026-07-16T11:00:00.000000"),
        artifact("aware-middle", "middle.txt", "2026-07-16T11:30:00+00:00"),
      ],
      recordings: [
        artifact("zulu-newest", "newest.webm", "2026-07-16T12:00:00.000Z"),
      ],
    });

    expect(timeline.map(({ checksum }) => checksum)).toEqual([
      "zulu-newest",
      "aware-middle",
      "naive-oldest",
    ]);
  });

  it("suppresses recordings while running", () => {
    const timeline = buildSessionTimeline({
      status: "running",
      downloadedFiles: [artifact("download", "download.txt", null)],
      recordings: [artifact("recording", "session.webm", null)],
    });
    expect(timeline.map(({ kind }) => kind)).toEqual(["download"]);
  });
});

describe("BrowserSessionTimeline", () => {
  it("renders nullable URL rows without links", async () => {
    renderTimeline({
      status: "completed",
      downloaded_files: [
        { checksum: "no-url", filename: null, modified_at: null, url: null },
        artifact("with-url", "report.csv", "2026-07-16T12:00:00.000Z"),
      ],
      recordings: [],
    });

    const row = (await screen.findByText("Download 1")).closest("li");
    expect(row).not.toBeNull();
    expect(within(row!).queryByRole("link")).toBeNull();
    expect(screen.getByRole("link", { name: "Open report.csv" })).toBeTruthy();
  });

  it("falls back to the unavailable label for invalid timestamps", async () => {
    renderTimeline({
      status: "completed",
      downloaded_files: [artifact("bad-time", "report.csv", "not-a-date")],
      recordings: [],
    });

    await screen.findByText("report.csv");
    expect(screen.getByText("Availability time unavailable")).toBeTruthy();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
  });

  it("proxies file:// recording URLs through the artifact server", async () => {
    renderTimeline({
      status: "completed",
      downloaded_files: [],
      recordings: [
        {
          checksum: "local-rec",
          filename: "local.webm",
          modified_at: "2026-07-16T12:00:00.000Z",
          url: "file:///data/videos/local.webm",
        },
      ],
    });

    const link = await screen.findByRole("link", { name: "Open local.webm" });
    expect(link.getAttribute("href")).toContain(
      `/artifact/recording?path=${encodeURIComponent("/data/videos/local.webm")}`,
    );
  });

  it("renders the empty state and deduplicates shared query subscribers", async () => {
    const get = renderTimeline(
      { status: "completed", downloaded_files: null, recordings: null },
      true,
    );
    const emptyState =
      "No artifacts available yet — downloads and recordings will appear here as they become available.";
    expect(await screen.findAllByText(emptyState)).toHaveLength(2);
    expect(get).toHaveBeenCalledTimes(1);
  });
});
