import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { getClient } from "@/api/AxiosClient";
import { basicLocalTimeFormat } from "@/util/timeFormat";

import { BrowserSessionTimeline } from "./BrowserSessionTimeline";
import {
  type ActionLogEvent,
  type ActionLogPage,
  buildSessionTimeline,
  getSessionTimelineKindLabel,
} from "./BrowserSessionTimeline.utils";

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

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

const actionEvent = (
  event_id: string,
  occurred_at: string,
  overrides: Partial<ActionLogEvent> = {},
): ActionLogEvent => ({
  schema_version: 1,
  event_id,
  tool: "skyvern_click",
  selector: null,
  value: null,
  source_url: "https://example.test/path",
  occurred_at,
  timing_ms: { total: 125 },
  outcome: "success",
  error_code: null,
  index: 0,
  artifact_ref: null,
  ...overrides,
});

const httpError = (status: number) =>
  Object.assign(new Error(`Request failed with status ${status}`), {
    isAxiosError: true,
    response: { status },
  });

type ActionLogPageGetter = (
  cursor: string | null,
) => ActionLogPage | Promise<ActionLogPage>;

function renderTimeline(
  browserSession: unknown,
  duplicate = false,
  getActionLogPage: ActionLogPageGetter = () => ({
    events: [],
    next_cursor: null,
  }),
  seedBrowserSession = false,
) {
  const actionLogGet = vi.fn(getActionLogPage);
  const get = vi.fn(
    async (url: string, options?: { params?: { cursor?: string } }) => ({
      data: url.endsWith("/action_logs")
        ? await actionLogGet(options?.params?.cursor ?? null)
        : browserSession,
    }),
  );
  vi.mocked(getClient).mockResolvedValue({ get } as never);
  const timeline = <BrowserSessionTimeline />;
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  if (seedBrowserSession) {
    queryClient.setQueryDefaults(["browserSession"], {
      staleTime: Infinity,
    });
    queryClient.setQueryData(["browserSession", "session-1"], browserSession);
    queryClient.setQueryData(["browserSessionActionLogs", "session-1", null], {
      events: [],
      next_cursor: null,
    });
  }

  render(
    <MemoryRouter initialEntries={["/browser-session/session-1/timeline"]}>
      <QueryClientProvider client={queryClient}>
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

  return { actionLogGet, get, queryClient };
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

    expect(
      timeline.map((item) =>
        item.kind === "action" ? item.event_id : item.checksum,
      ),
    ).toEqual([
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
    expect(timeline[1]?.kind === "action" ? null : timeline[1]?.filename).toBe(
      "report.csv",
    );
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

    expect(
      timeline.map((item) =>
        item.kind === "action" ? item.event_id : item.checksum,
      ),
    ).toEqual(["zulu-newest", "aware-middle", "naive-oldest"]);
  });

  it("suppresses recordings while running", () => {
    const timeline = buildSessionTimeline({
      status: "running",
      downloadedFiles: [artifact("download", "download.txt", null)],
      recordings: [artifact("recording", "session.webm", null)],
    });
    expect(timeline.map(({ kind }) => kind)).toEqual(["download"]);
  });

  it("orders actions by event time and id with stable artifact interleaving", () => {
    const sameTime = "2026-07-16T11:00:00.000Z";
    const timeline = buildSessionTimeline({
      status: "completed",
      downloadedFiles: [artifact("download", "report.csv", sameTime)],
      recordings: [],
      actionEvents: [
        actionEvent("event-b", sameTime),
        actionEvent("event-newest", "2026-07-16T12:00:00.000Z"),
        actionEvent("event-a", sameTime),
      ],
    });

    expect(
      timeline.map((item) =>
        item.kind === "action" ? item.event_id : item.checksum,
      ),
    ).toEqual(["event-newest", "event-a", "event-b", "download"]);
  });

  it("breaks same-millisecond ties by action index before event id", () => {
    const sameTime = "2026-07-16T11:00:00.000Z";
    const timeline = buildSessionTimeline({
      status: "completed",
      downloadedFiles: [],
      recordings: [],
      actionEvents: [
        actionEvent("event-a", sameTime),
        actionEvent("event-b", sameTime, { index: 1 }),
      ],
    });

    expect(
      timeline.map((item) => (item.kind === "action" ? item.event_id : null)),
    ).toEqual(["event-b", "event-a"]);
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

  it("renders action tool, outcome, duration, and event time", async () => {
    const occurredAt = "2026-07-16T12:00:00.000Z";
    renderTimeline(
      { status: "completed", downloaded_files: [], recordings: [] },
      false,
      () => ({
        events: [actionEvent("event-1", occurredAt)],
        next_cursor: null,
      }),
    );

    const row = (await screen.findByText("skyvern_click")).closest("li");
    expect(row).not.toBeNull();
    expect(within(row!).getByText("Action")).toBeTruthy();
    expect(within(row!).getByText("success")).toBeTruthy();
    expect(within(row!).getByText("125 ms")).toBeTruthy();
    expect(
      within(row!).getByText(basicLocalTimeFormat(occurredAt)),
    ).toBeTruthy();
    expect(within(row!).queryByRole("link")).toBeNull();
  });

  it("renders downloads and recordings and stops polling after action-log 404", async () => {
    vi.useFakeTimers();
    const { actionLogGet } = renderTimeline(
      {
        status: "created",
        downloaded_files: [
          artifact("download", "report.csv", "2026-07-16T12:00:00.000Z"),
        ],
        recordings: [
          artifact("recording", "session.webm", "2026-07-16T12:01:00.000Z"),
        ],
      },
      false,
      () => Promise.reject(httpError(404)),
    );

    await vi.waitFor(() => expect(screen.getByText("report.csv")).toBeTruthy());
    expect(screen.getByText("session.webm")).toBeTruthy();
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(actionLogGet).toHaveBeenCalledTimes(1);
  });

  it("renders downloads and recordings when action logs return 500", async () => {
    renderTimeline(
      {
        status: "completed",
        downloaded_files: [
          artifact("download", "report.csv", "2026-07-16T12:00:00.000Z"),
        ],
        recordings: [
          artifact("recording", "session.webm", "2026-07-16T12:01:00.000Z"),
        ],
      },
      false,
      () => Promise.reject(httpError(500)),
    );

    expect(await screen.findByText("report.csv")).toBeTruthy();
    expect(screen.getByText("session.webm")).toBeTruthy();
  });

  it("polls the latest cursor while running and deduplicates merged pages", async () => {
    const first = actionEvent("event-1", "2026-07-16T11:00:00.000Z");
    const second = actionEvent("event-2", "2026-07-16T12:00:00.000Z", {
      tool: "skyvern_type_text",
    });
    let cursorReads = 0;
    const { actionLogGet } = renderTimeline(
      { status: "running", downloaded_files: [], recordings: [] },
      false,
      (cursor) => {
        if (cursor === null) {
          return { events: [first], next_cursor: "cursor-1" };
        }
        cursorReads += 1;
        return cursorReads === 1
          ? { events: [first], next_cursor: null }
          : { events: [first, second], next_cursor: null };
      },
    );

    expect(await screen.findByText("skyvern_click")).toBeTruthy();
    expect(actionLogGet).toHaveBeenCalledWith(null);
    expect(actionLogGet).toHaveBeenCalledWith("cursor-1");

    expect(
      await screen.findByText("skyvern_type_text", {}, { timeout: 2000 }),
    ).toBeTruthy();
    expect(screen.getAllByText("skyvern_click")).toHaveLength(1);
    expect(actionLogGet).toHaveBeenLastCalledWith("cursor-1");
  });

  it("starts a distinct tail fetch after the active request settles post-terminal", async () => {
    const tail = actionEvent("event-tail", "2026-07-16T12:00:00.000Z", {
      tool: "skyvern_type_text",
    });
    let resolveFirstPage!: (page: ActionLogPage) => void;
    const firstPage = new Promise<ActionLogPage>((resolve) => {
      resolveFirstPage = resolve;
    });
    let actionReads = 0;
    const { actionLogGet, queryClient } = renderTimeline(
      { status: "created", downloaded_files: [], recordings: [] },
      false,
      () =>
        actionReads++ === 0 ? firstPage : { events: [tail], next_cursor: null },
      true,
    );

    await vi.waitFor(() => expect(actionLogGet).toHaveBeenCalledTimes(1));
    await vi.waitFor(() =>
      expect(
        queryClient.getQueryData(["browserSession", "session-1"]),
      ).toMatchObject({ status: "created" }),
    );
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    await act(async () => {
      queryClient.setQueryData(["browserSession", "session-1"], {
        status: "completed",
        downloaded_files: [],
        recordings: [],
      });
    });
    expect(actionLogGet).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveFirstPage({ events: [], next_cursor: null });
    });

    await vi.waitFor(() => expect(actionLogGet).toHaveBeenCalledTimes(2), {
      timeout: 250,
    });
    expect(await screen.findByText("skyvern_type_text")).toBeTruthy();
  });

  it("fetches the drained tail when the session turns terminal while idle", async () => {
    const tail = actionEvent("event-tail-idle", "2026-07-16T12:00:00.000Z", {
      tool: "skyvern_type_text",
    });
    let actionReads = 0;
    const { actionLogGet, queryClient } = renderTimeline(
      { status: "created", downloaded_files: [], recordings: [] },
      false,
      () =>
        actionReads++ === 0
          ? { events: [], next_cursor: null }
          : { events: [tail], next_cursor: null },
      true,
    );

    await vi.waitFor(() => expect(actionLogGet).toHaveBeenCalledTimes(1));
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));

    await act(async () => {
      queryClient.setQueryData(["browserSession", "session-1"], {
        status: "completed",
        downloaded_files: [],
        recordings: [],
      });
    });

    await vi.waitFor(() => expect(actionLogGet).toHaveBeenCalledTimes(2), {
      timeout: 250,
    });
    expect(await screen.findByText("skyvern_type_text")).toBeTruthy();
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
    const { get } = renderTimeline(
      { status: "completed", downloaded_files: null, recordings: null },
      true,
    );
    const emptyState =
      "No timeline events available yet — actions, downloads, and recordings will appear here as they become available.";
    expect(await screen.findAllByText(emptyState)).toHaveLength(2);
    expect(get).toHaveBeenCalledTimes(2);
  });
});
