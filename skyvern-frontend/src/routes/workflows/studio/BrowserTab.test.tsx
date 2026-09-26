// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ActionTypes, Status, type ActionsApiResponse } from "@/api/types";
import type { StreamStateChangeHandler } from "@/routes/streaming/streamState";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useRecordingLauncherStore } from "@/store/useRecordingLauncherStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { useRunViewStore } from "@/store/RunViewStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { compactLocalDateTime } from "@/util/timeFormat";

import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import { BrowserPaneActions, BrowserPaneViewPills } from "./BrowserPaneHeader";
import { BrowserTab } from "./BrowserTab";
import { StudioShellContext } from "./StudioShellContext";

const mocks = vi.hoisted(() => ({
  workflowRun: undefined as unknown,
  timeline: undefined as unknown,
  debugSession: undefined as unknown,
  runs: [] as Array<{ workflow_run_id: string }>,
  realScreenshot: false,
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({ get: async () => ({ data: [] }) }),
}));

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: (options?: { workflowRunId?: string }) => ({
    data: options?.workflowRunId ? mocks.workflowRun : undefined,
  }),
}));
vi.mock("../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: (options?: { workflowRunId?: string }) => ({
    data: options?.workflowRunId ? mocks.timeline : undefined,
  }),
}));
vi.mock("../hooks/useDebugSessionQuery", () => ({
  useDebugSessionQuery: () => ({ data: mocks.debugSession }),
}));
vi.mock("../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => ({ data: mocks.runs, isPending: false }),
}));
vi.mock("@/hooks/useRuntimeConfig", () => ({
  useBrowserStreamingMode: () => ({ browserStreamingMode: "vnc" }),
  useStreamTransport: () => ({ streamTransport: "vnc" }),
}));
vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn() }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/routes/streaming/StreamDiagnostics", () => ({
  StreamModeBadge: ({ mode }: { mode: string }) => (
    <span data-testid="stream-mode-badge">{mode}</span>
  ),
  StreamStatusPanel: ({
    diagnostic,
  }: {
    diagnostic: { title: string; detail?: string };
  }) => (
    <div data-testid="stream-status">
      <span>{diagnostic.title}</span>
      <span>{diagnostic.detail}</span>
    </div>
  ),
}));
vi.mock("./runview/HeroRecording", () => ({
  HeroRecording: ({ recordingUrls }: { recordingUrls: string[] }) => (
    <div data-testid="hero-recording" data-count={recordingUrls.length} />
  ),
}));
vi.mock("./runview/HeroScreenshot", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("./runview/HeroScreenshot")>();
  return {
    HeroScreenshot: (props: Parameters<typeof actual.HeroScreenshot>[0]) =>
      mocks.realScreenshot ? (
        <actual.HeroScreenshot {...props} />
      ) : (
        <div
          data-testid="hero-screenshot"
          data-selection={JSON.stringify(props.selection)}
        />
      ),
  };
});
vi.mock("./runview/RunLiveStream", () => ({
  RunLiveStream: (props: {
    workflowRunId: string;
    browserSessionId: string | null;
    interactive: boolean;
    onStreamStateChange?: StreamStateChangeHandler;
  }) => (
    <div
      data-testid="run-live-stream"
      data-run={props.workflowRunId}
      data-session={props.browserSessionId ?? ""}
      data-interactive={props.interactive ? "yes" : "no"}
    >
      {(["live", "stopped"] as const).map((state) => (
        <button
          key={state}
          type="button"
          onClick={() =>
            props.onStreamStateChange?.(state, props.browserSessionId)
          }
        >
          emit run {state}
        </button>
      ))}
    </div>
  ),
}));
const initialBrowserState = useStudioBrowserStore.getState();
const initialRunViewState = useRunViewStore.getState();
const initialRecordingState = useRecordingStore.getState();
const initialSettingsState = useSettingsStore.getState();
const initialRecordingLauncherState = useRecordingLauncherStore.getState();

function buildBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_1",
    workflow_run_id: "wr_1",
    parent_workflow_run_block_id: null,
    block_type: "task",
    label: "Go to portal",
    description: null,
    title: null,
    status: Status.Completed,
    failure_reason: null,
    output: null,
    continue_on_failure: false,
    task_id: null,
    url: null,
    navigation_goal: null,
    navigation_payload: null,
    data_extraction_goal: null,
    data_schema: null,
    terminate_criterion: null,
    complete_criterion: null,
    include_action_history_in_verification: null,
    engine: null,
    actions: null,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    duration: null,
    loop_values: null,
    current_value: null,
    current_index: null,
    ...overrides,
  };
}

function buildAction(
  overrides: Partial<ActionsApiResponse> = {},
): ActionsApiResponse {
  return {
    action_id: "act_1",
    action_type: ActionTypes.Click,
    status: Status.Completed,
    intention: "Click the login button",
    description: null,
    reasoning: null,
    step_id: "step_1",
    action_order: 0,
    screenshot_artifact_id: "art_1",
    ...overrides,
  } as ActionsApiResponse;
}

function buildBlockItem(
  block: WorkflowRunBlock,
  children: Array<WorkflowRunTimelineItem> = [],
): WorkflowRunTimelineItem {
  return {
    type: "block",
    block,
    children,
    thought: null,
    created_at: block.created_at,
    modified_at: block.modified_at,
  };
}

function seedRun({
  status,
  browserSessionId = null,
  recordingUrl = null,
}: {
  status: Status;
  browserSessionId?: string | null;
  recordingUrl?: string | null;
}) {
  mocks.runs = [{ workflow_run_id: "wr_1" }];
  mocks.workflowRun = {
    workflow_run_id: "wr_1",
    status,
    created_at: "2026-09-24T12:00:00",
    browser_session_id: browserSessionId,
    recording_url: recordingUrl,
    recording_urls: recordingUrl ? [recordingUrl] : null,
    workflow: {
      workflow_definition: { blocks: [], finally_block_label: null },
    },
  };
  mocks.timeline = [buildBlockItem(buildBlock({ actions: [buildAction()] }))];
}

function renderBrowserPane(initialPath: string) {
  const setBrowserStreamSlot = vi.fn();
  const queryClient = new QueryClient();
  const pane = () => (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={0}>
        <StudioShellContext.Provider
          value={{
            copilotPortalEl: null,
            panelPortalEl: null,
            setEditorStreamSlot: () => {},
            setBrowserStreamSlot,
            setRunStreamSlot: () => {},
          }}
        >
          <MemoryRouter initialEntries={[initialPath]}>
            <Routes>
              <Route
                path="/workflows/:workflowPermanentId/studio"
                element={
                  <>
                    <div data-testid="pane-header">
                      <BrowserPaneViewPills />
                      <BrowserPaneActions />
                    </div>
                    <BrowserTab />
                    <LocationProbe />
                  </>
                }
              />
            </Routes>
          </MemoryRouter>
        </StudioShellContext.Provider>
      </TooltipProvider>
    </QueryClientProvider>
  );
  const view = render(pane());
  return {
    ...view,
    setBrowserStreamSlot,
    queryClient,
    rerenderPane: () => view.rerender(pane()),
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
}

const STUDIO_PATH = "/workflows/wpid_test/studio?panes=copilot,browser";

beforeEach(() => {
  useStudioBrowserStore.setState(initialBrowserState, true);
  useRunViewStore.setState(initialRunViewState, true);
  useRecordingStore.setState(initialRecordingState, true);
  useSettingsStore.setState(initialSettingsState, true);
  useRecordingLauncherStore.setState(initialRecordingLauncherState, true);
  mocks.workflowRun = undefined;
  mocks.timeline = undefined;
  mocks.debugSession = undefined;
  mocks.runs = [];
  mocks.realScreenshot = false;
});

afterEach(() => {
  cleanup();
});

describe("BrowserTab view machine", () => {
  it("keeps the recording view synchronized with the URL", async () => {
    seedRun({
      status: Status.Completed,
      recordingUrl: "https://example.com/recording.webm",
    });
    renderBrowserPane("/workflows/wpid_test/studio?wr=wr_1&view=recording");

    await waitFor(() => {
      expect(
        screen
          .getByRole("button", { name: "Recording" })
          .getAttribute("aria-pressed"),
      ).toBe("true");
    });

    fireEvent.click(screen.getByRole("button", { name: "Screenshots" }));
    await waitFor(() => {
      expect(screen.getByTestId("location-search").textContent).not.toContain(
        "view=recording",
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "Recording" }));
    await waitFor(() => {
      expect(screen.getByTestId("location-search").textContent).toContain(
        "view=recording",
      );
      expect(screen.getByTestId("location-search").textContent).not.toContain(
        "panes=",
      );
    });
  });

  it("keeps a recording deep link pinned when frame hydration updates", async () => {
    seedRun({ status: Status.Failed });
    renderBrowserPane(
      "/workflows/wpid_test/studio?wr=wr_1&active=wrb_1&view=recording",
    );

    await waitFor(() => {
      expect(
        screen
          .getByRole("button", { name: "Recording" })
          .getAttribute("aria-pressed"),
      ).toBe("true");
    });

    act(() => useRunViewStore.getState().pinFrame("wrb_1"));

    await waitFor(() => {
      expect(
        screen
          .getByRole("button", { name: "Recording" })
          .getAttribute("aria-pressed"),
      ).toBe("true");
    });
  });

  it("keeps Live selected during a retry wait and pauses artifact polling until execution resumes", () => {
    mocks.realScreenshot = true;
    seedRun({ status: Status.Running });
    const block = buildBlock({
      actions: [buildAction({ screenshot_artifact_id: null })],
    });
    mocks.timeline = [buildBlockItem(block)];
    const { queryClient, rerenderPane } = renderBrowserPane(
      `${STUDIO_PATH}&wr=wr_1`,
    );
    const expectLive = () =>
      expect(
        screen
          .getByRole("button", { name: "Live" })
          .getAttribute("aria-pressed"),
      ).toBe("true");
    const expectArtifactPolling = (interval: number | false) => {
      const queries = queryClient
        .getQueryCache()
        .getAll()
        .filter(
          (query) => query.queryKey[query.queryKey.length - 1] === "artifacts",
        );
      expect(queries).toHaveLength(3);
      for (const query of queries) {
        expect(query.observers[0]?.options.refetchInterval).toBe(interval);
      }
      expect(
        queries.find((query) => query.queryKey[0] === "step")?.observers[0]
          ?.options.enabled,
      ).toBe(true);
    };

    expectLive();
    mocks.workflowRun = Object.assign({}, mocks.workflowRun, {
      status: Status.Failed,
      retry_pending: true,
    });
    rerenderPane();
    expectLive();
    expect(
      screen.getByText("The browser reconnects when the next attempt starts."),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Screenshots" }));
    expectArtifactPolling(false);
    fireEvent.click(screen.getByRole("button", { name: "Live" }));

    mocks.workflowRun = Object.assign({}, mocks.workflowRun, {
      status: Status.Running,
      retry_pending: false,
      attempt: 2,
    });
    mocks.timeline = [{ ...buildBlockItem(block), attempt: 2 }];
    rerenderPane();
    expectLive();
    expect(screen.getByTestId("run-live-stream")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Screenshots" }));
    expectArtifactPolling(5000);
  });

  it("keeps historical screenshots and recordings accessible during a retry wait", () => {
    seedRun({ status: Status.Completed, recordingUrl: "https://r.test/1.mp4" });
    mocks.workflowRun = Object.assign({}, mocks.workflowRun, {
      retry_pending: true,
      attempt: 2,
    });
    mocks.timeline = [
      {
        ...buildBlockItem(
          buildBlock({ workflow_run_block_id: "wrb_historical" }),
        ),
        attempt: 1,
      },
    ];
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1&active=wrb_historical`);

    const retryMessage = "The browser reconnects when the next attempt starts.";
    expect(screen.queryByText(retryMessage)).toBeNull();
    expect(
      JSON.parse(
        screen.getByTestId("hero-screenshot").getAttribute("data-selection") ??
          "{}",
      ),
    ).toEqual({
      kind: "block",
      workflowRunBlockId: "wrb_historical",
      blockType: "task",
    });

    fireEvent.click(screen.getByRole("button", { name: "Recording" }));
    expect(screen.getByTestId("hero-recording")).toBeTruthy();
    expect(screen.queryByText(retryMessage)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Screenshots" }));
    expect(screen.getByTestId("hero-screenshot")).toBeTruthy();
    expect(screen.queryByText(retryMessage)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Live" }));
    expect(screen.getByText(retryMessage)).toBeTruthy();
    expect(screen.queryByTestId("run-live-stream")).toBeNull();
  });

  it("shows the live debug stream slot with no run history", () => {
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(STUDIO_PATH);

    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();
    // No run open: Recording/Screenshots would replay nothing, so the whole
    // view switcher is gone rather than disabled.
    expect(screen.queryByRole("group", { name: "Browser view" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Screenshots" })).toBeNull();
  });

  it("never strands a zero-run workflow on the screenshots empty state", () => {
    // A pill intent left over from a run that was open earlier in the session.
    useStudioBrowserStore.setState({ view: "screenshots" });
    renderBrowserPane(STUDIO_PATH);

    expect(screen.queryByText("Waiting for the first action")).toBeNull();
    expect(screen.getByTestId("stream-status").textContent).toContain(
      "Warming up your browser",
    );
  });

  it("labels the replay pills with the run they replay", () => {
    seedRun({ status: Status.Completed });
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    // Same formatter as the Runs list, so the label is locale/TZ-stable here.
    expect(screen.getByTestId("browser-pane-run-label").textContent).toBe(
      `Run · ${compactLocalDateTime("2026-09-24T12:00:00")}`,
    );
    expect(screen.getByRole("button", { name: "Screenshots" })).toBeTruthy();
  });

  it("registers the shell stream slot while live", () => {
    mocks.debugSession = { browser_session_id: "pbs_test" };
    const { setBrowserStreamSlot } = renderBrowserPane(STUDIO_PATH);

    expect(setBrowserStreamSlot).toHaveBeenCalledWith(expect.any(HTMLElement));
  });

  it("shows the warming panel with no session and no history", () => {
    renderBrowserPane(STUDIO_PATH);

    expect(screen.getByTestId("stream-status").textContent).toContain(
      "Warming up your browser",
    );
  });

  it("edit entry stays on the (booting) live surface, not the latest run's replay", () => {
    // The latest run has a recording but the URL names no run: the pane must
    // come up live (warming) instead of flashing the replay while the debug
    // session boots.
    seedRun({ status: Status.Completed, recordingUrl: "https://r.test/1.mp4" });
    renderBrowserPane(STUDIO_PATH);

    expect(screen.queryByTestId("hero-recording")).toBeNull();
    expect(screen.getByTestId("stream-status").textContent).toContain(
      "Warming up your browser",
    );
    expect(screen.queryByRole("button", { name: "Recording" })).toBeNull();
  });

  it("prefers the live debug browser over an old run's replay when idle", () => {
    seedRun({ status: Status.Completed, recordingUrl: "https://r.test/1.mp4" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(STUDIO_PATH);

    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();
  });

  it("replays a finished run named in the URL even while the session is live", () => {
    seedRun({ status: Status.Completed, recordingUrl: "https://r.test/1.mp4" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    expect(screen.getByTestId("hero-recording")).toBeTruthy();
  });

  it("keeps a finished Copilot-focused run that ran in the debug session on the live debug browser", () => {
    seedRun({ status: Status.Completed, browserSessionId: "pbs_test" });
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          actions: [buildAction({ screenshot_artifact_id: null })],
        }),
      ),
    ];
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1&wrs=copilot`);

    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();
    expect(screen.queryByTestId("hero-recording")).toBeNull();
    expect(screen.queryByTestId("hero-screenshot")).toBeNull();
  });

  it("replays a finished Copilot-focused run that ran in its own browser", () => {
    seedRun({
      status: Status.Completed,
      browserSessionId: "pbs_run",
      recordingUrl: "https://r.test/1.mp4",
    });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1&wrs=copilot`);

    expect(screen.getByTestId("hero-recording")).toBeTruthy();
    expect(screen.queryByTestId("browser-pane-stream-slot")).toBeNull();
  });

  it("shows the inspected step's screenshot when ?active= is set", () => {
    seedRun({ status: Status.Completed });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1&wrs=copilot&active=act_1`);

    const shot = screen.getByTestId("hero-screenshot");
    expect(JSON.parse(shot.getAttribute("data-selection") ?? "{}")).toEqual({
      kind: "action",
      artifactId: "art_1",
      stepId: "step_1",
      actionOrder: 0,
    });
  });

  it("stays live for an executing block run in the debug session", () => {
    seedRun({ status: Status.Running, browserSessionId: "pbs_test" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1&bl=Block%201`);

    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();
    expect(screen.queryByTestId("run-live-stream")).toBeNull();
  });

  it("keeps the live debug browser after a block run finalizes", () => {
    seedRun({
      status: Status.Completed,
      browserSessionId: "pbs_test",
      recordingUrl: "https://r.test/1.mp4",
    });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1&bl=Block%201`);

    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();
  });

  it("streams a running full run through its own run stream", () => {
    seedRun({ status: Status.Running, browserSessionId: "pbs_run" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    const stream = screen.getByTestId("run-live-stream");
    expect(stream.getAttribute("data-run")).toBe("wr_1");
    expect(stream.getAttribute("data-session")).toBe("pbs_run");
    expect(stream.getAttribute("data-interactive")).toBe("no");
    expect(screen.queryByTestId("browser-pane-stream-slot")).toBeNull();
  });

  it("follows the latest run's own stream in the edit-context status", () => {
    // No run in the URL, but the latest run is running outside the debug
    // session, so Live streams that run; a running run alone isn't Live.
    seedRun({ status: Status.Running, browserSessionId: "pbs_run" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(STUDIO_PATH);

    const status = screen.getByTestId("browser-pane-live-status");
    expect(status.textContent).toBe("Starting browser…");
    expect(screen.getByTestId("browser-pane-run-label")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Screenshots" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "emit run live" }));
    expect(status.textContent).toBe("Live");

    fireEvent.click(screen.getByRole("button", { name: "emit run stopped" }));
    expect(status.textContent).toBe("Browser stopped");
  });

  it("ignores stream state reported for a different run", () => {
    seedRun({ status: Status.Running, browserSessionId: "pbs_run" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    useStudioBrowserStore.getState().setRunStreamState("live", "wr_other");
    renderBrowserPane(STUDIO_PATH);

    expect(screen.getByTestId("browser-pane-live-status").textContent).toBe(
      "Starting browser…",
    );
  });

  it("says the latest run's browser is starting while that run is queued", () => {
    seedRun({ status: Status.Queued, browserSessionId: "pbs_run" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(STUDIO_PATH);

    expect(screen.getByTestId("stream-status").textContent).toContain(
      "Starting the browser",
    );
    expect(screen.getByTestId("browser-pane-live-status").textContent).toBe(
      "Starting browser…",
    );
  });

  it("shows the editing browser's status, not run tabs, while editing", () => {
    seedRun({ status: Status.Completed });
    useSettingsStore.setState({ isLoadingABrowser: true });
    renderBrowserPane(STUDIO_PATH);

    expect(screen.getByTestId("browser-pane-live-status").textContent).toBe(
      "Starting browser…",
    );

    // In studio the route's loading flag never clears, and a run's own stream
    // leaves the global "using a browser" flag behind; only the studio
    // stream's readiness for this session means the browser is up.
    mocks.debugSession = { browser_session_id: "pbs_test" };
    act(() =>
      useSettingsStore.setState({
        isUsingABrowser: true,
        browserSessionId: "pbs_run",
      }),
    );
    expect(screen.getByTestId("browser-pane-live-status").textContent).toBe(
      "Starting browser…",
    );

    act(() =>
      useStudioBrowserStore.getState().setDebugStreamState("live", "pbs_test"),
    );
    expect(screen.getByTestId("browser-pane-live-status").textContent).toBe(
      "Live",
    );
    expect(screen.queryByTestId("browser-pane-run-label")).toBeNull();
    expect(screen.queryByRole("group", { name: "Browser view" })).toBeNull();
  });

  it("says the editing browser stopped once its stream gives up", () => {
    seedRun({ status: Status.Completed });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(STUDIO_PATH);

    act(() =>
      useStudioBrowserStore
        .getState()
        .setDebugStreamState("stopped", "pbs_test"),
    );
    const status = screen.getByRole("status");
    expect(status.textContent).toBe("Browser stopped");
    expect(status.getAttribute("title")).toContain("Restart browser");
  });

  it("makes a paused run's stream interactive (human input)", () => {
    seedRun({ status: Status.Paused, browserSessionId: "pbs_run" });
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    expect(
      screen.getByTestId("run-live-stream").getAttribute("data-interactive"),
    ).toBe("yes");
  });

  it("waits for a queued full run instead of mounting its stream", () => {
    seedRun({ status: Status.Queued });
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    expect(screen.getByTestId("stream-status").textContent).toContain(
      "Starting the browser",
    );
    expect(screen.queryByTestId("run-live-stream")).toBeNull();
  });

  it("drops a stale replay pill when a recording starts", () => {
    seedRun({ status: Status.Completed, recordingUrl: "https://r.test/1.mp4" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    // The user pinned the replay before hitting Record.
    useStudioBrowserStore.setState({ view: "recording" });
    useRecordingStore.setState({ isRecording: true });
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();
    expect(screen.queryByTestId("hero-recording")).toBeNull();
  });

  it("pins the live debug stream while a browser recording runs", () => {
    // Even with a pinned step (?active=), recording surfaces the live browser.
    seedRun({ status: Status.Completed });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    useRecordingStore.setState({ isRecording: true });
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1&active=act_1`);

    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stop recording" })).toBeNull();
  });

  it("starts a browser recording from the studio browser header", () => {
    const startRecording = vi.fn();
    mocks.debugSession = { browser_session_id: "pbs_test" };
    useRecordingLauncherStore.setState({
      startRecordingAtEnd: startRecording,
    });
    renderBrowserPane(STUDIO_PATH);

    const recordTaskButton = screen.getByRole("button", {
      name: "Record task",
    });
    expect(recordTaskButton.textContent).toContain("Record task");
    fireEvent.click(recordTaskButton);

    expect(startRecording).toHaveBeenCalledOnce();
  });

  it("a pinned Recording view without a recording shows the empty state", () => {
    seedRun({ status: Status.Completed });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    fireEvent.click(screen.getByRole("button", { name: "Recording" }));
    expect(screen.getByText("No recording for this run")).toBeTruthy();
  });

  it("keeps an archived recording's pill live and explains it in the body", () => {
    seedRun({ status: Status.Completed });
    mocks.workflowRun = Object.assign({}, mocks.workflowRun, {
      recording_archived: true,
    });
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    const pill = screen.getByRole("button", { name: "Recording" });
    expect((pill as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(pill);
    expect(screen.getByText("Recording archived")).toBeTruthy();
  });

  it("opens the recording requested by a legacy deep link", () => {
    seedRun({
      status: Status.Failed,
      recordingUrl: "https://r.test/1.mp4",
    });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1&view=recording`);

    expect(screen.getByTestId("hero-recording")).toBeTruthy();
    expect(useStudioBrowserStore.getState().view).toBe("recording");
  });

  it("flags a queued block run on the live debug stream", () => {
    seedRun({ status: Status.Queued, browserSessionId: "pbs_test" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1&bl=Block%201`);

    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();
    expect(screen.getByText(/Run queued/)).toBeTruthy();
  });
});

describe("BrowserTab pills and selection sync", () => {
  it("keeps explicit view controls authoritative during Copilot focus", () => {
    seedRun({
      status: Status.Completed,
      browserSessionId: "pbs_test",
      recordingUrl: "https://r.test/1.mp4",
    });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1&wrs=copilot`);

    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Screenshots" }));
    expect(screen.getByTestId("hero-screenshot")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Recording" }));
    expect(screen.getByTestId("hero-recording")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Debug browser" }));
    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();
  });

  it("switches views from the header pills", () => {
    seedRun({ status: Status.Completed, recordingUrl: "https://r.test/1.mp4" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    expect(screen.getByTestId("hero-recording")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Debug browser" }));
    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Screenshots" }));
    expect(screen.getByTestId("hero-screenshot")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Recording" }));
    expect(screen.getByTestId("hero-recording")).toBeTruthy();
  });

  it("returns to the selected step when the timeline re-pins it", () => {
    seedRun({ status: Status.Completed });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1&active=act_1`);

    expect(screen.getByTestId("hero-screenshot")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Debug browser" }));
    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();

    // A timeline click pins the frame (even the already-selected one); the
    // pane hands the view back to the machine, which lands on Screenshots.
    act(() => {
      useRunViewStore.getState().pinFrame("act_1");
    });
    expect(screen.getByTestId("hero-screenshot")).toBeTruthy();
  });

  it("hides the debug-browser menu while the run's own stream is shown", () => {
    seedRun({ status: Status.Running, browserSessionId: "pbs_run" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    useRecordingLauncherStore.setState({ startRecordingAtEnd: vi.fn() });
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    expect(screen.getByTestId("run-live-stream")).toBeTruthy();
    expect(
      (screen.getByLabelText("Record task") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      screen.queryByRole("button", { name: "More browser actions" }),
    ).toBeNull();
  });

  it("keeps session actions in the overflow menu, with Restart destructive and confirmed", async () => {
    mocks.debugSession = { browser_session_id: "pbs_test" };
    const reload = vi.fn();
    useStudioBrowserStore.setState({ reload });
    renderBrowserPane(STUDIO_PATH);

    expect(screen.queryByLabelText("Reconnect browser stream")).toBeNull();
    const openMenu = () =>
      fireEvent.pointerDown(
        screen.getByRole("button", { name: "More browser actions" }),
        { button: 0, ctrlKey: false },
      );

    openMenu();
    for (const name of ["Reconnect stream", "Open in new tab"]) {
      expect(
        (await screen.findByRole("menuitem", { name })).className,
      ).not.toContain("text-destructive");
    }
    fireEvent.click(screen.getByRole("menuitem", { name: "Reconnect stream" }));
    expect(reload).toHaveBeenCalledOnce();

    openMenu();
    const restart = await screen.findByRole("menuitem", {
      name: "Restart browser…",
    });
    expect(restart.className).toContain("text-destructive");
    fireEvent.click(restart);
    expect(await screen.findByText("Restart this browser?")).toBeTruthy();

    // Keyboard users land back on the ⋯ that opened the dialog, not <body>.
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: "More browser actions" }),
      );
    });
  });

  it("marks the resolved view's pill as pressed", () => {
    seedRun({ status: Status.Completed, recordingUrl: "https://r.test/1.mp4" });
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    expect(
      screen
        .getByRole("button", { name: "Recording" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(
      screen
        .getByRole("button", { name: "Debug browser" })
        .getAttribute("aria-pressed"),
    ).toBe("false");
  });

  it("never offers a finished run a Live pill", () => {
    seedRun({ status: Status.Completed, recordingUrl: "https://r.test/1.mp4" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    // Beside the run's own Recording/Screenshots pills, a pulsing "Live" reads
    // as the run's status — but this view is the debug browser the run left.
    expect(screen.queryByRole("button", { name: "Live" })).toBeNull();
    const pill = screen.getByRole("button", { name: "Debug browser" });
    expect(pill.querySelector(".animate-pulse")).toBeNull();
  });

  it("keeps the Live pill while the inspected run is still running", () => {
    seedRun({ status: Status.Running, browserSessionId: "pbs_run" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    expect(screen.getByRole("button", { name: "Live" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Debug browser" })).toBeNull();
  });
});

describe("stream-mode badge", () => {
  it("does not show the transport badge in the browser pane", () => {
    renderBrowserPane(STUDIO_PATH);
    expect(screen.queryByTestId("stream-mode-badge")).toBeNull();
  });
});

it.each([false, true])(
  "keeps the shared browser visible during a retry wait (recording=%s)",
  (recording) => {
    seedRun({ status: Status.Failed, browserSessionId: "pbs_test" });
    mocks.workflowRun = Object.assign({}, mocks.workflowRun, {
      retry_pending: true,
    });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    useRecordingStore.setState({ isRecording: recording });
    renderBrowserPane(recording ? `${STUDIO_PATH}&wr=wr_1` : STUDIO_PATH);
    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();
    expect(screen.queryByText("Retry pending")).toBeNull();
  },
);
