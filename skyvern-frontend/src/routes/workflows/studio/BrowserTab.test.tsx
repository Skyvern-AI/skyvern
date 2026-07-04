// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ActionTypes, Status, type ActionsApiResponse } from "@/api/types";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useRunViewStore } from "@/store/RunViewStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

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
  StreamStatusPanel: ({ diagnostic }: { diagnostic: { title: string } }) => (
    <div data-testid="stream-status">{diagnostic.title}</div>
  ),
}));
vi.mock("./runview/HeroRecording", () => ({
  HeroRecording: ({ recordingUrls }: { recordingUrls: string[] }) => (
    <div data-testid="hero-recording" data-count={recordingUrls.length} />
  ),
}));
vi.mock("./runview/HeroScreenshot", () => ({
  HeroScreenshot: ({ selection }: { selection: unknown }) => (
    <div
      data-testid="hero-screenshot"
      data-selection={JSON.stringify(selection)}
    />
  ),
}));
vi.mock("./runview/RunLiveStream", () => ({
  RunLiveStream: (props: {
    workflowRunId: string;
    browserSessionId: string | null;
    interactive: boolean;
  }) => (
    <div
      data-testid="run-live-stream"
      data-run={props.workflowRunId}
      data-session={props.browserSessionId ?? ""}
      data-interactive={props.interactive ? "yes" : "no"}
    />
  ),
}));

const initialBrowserState = useStudioBrowserStore.getState();
const initialRunViewState = useRunViewStore.getState();
const initialRecordingState = useRecordingStore.getState();

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
  const view = render(
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
                  </>
                }
              />
            </Routes>
          </MemoryRouter>
        </StudioShellContext.Provider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return { ...view, setBrowserStreamSlot };
}

const STUDIO_PATH = "/workflows/wpid_test/studio?panes=copilot,browser";

beforeEach(() => {
  useStudioBrowserStore.setState(initialBrowserState, true);
  useRunViewStore.setState(initialRunViewState, true);
  useRecordingStore.setState(initialRecordingState, true);
  mocks.workflowRun = undefined;
  mocks.timeline = undefined;
  mocks.debugSession = undefined;
  mocks.runs = [];
});

afterEach(() => {
  cleanup();
});

describe("BrowserTab view machine", () => {
  it("shows the live debug stream slot with no run history", () => {
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(STUDIO_PATH);

    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Live" }).getAttribute("aria-pressed"),
    ).toBe("true");
    // The replay pills stay visible even with nothing to replay; their views
    // render empty states instead of the pills disappearing.
    expect(screen.getByRole("button", { name: "Recording" })).toBeTruthy();
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
    expect(
      screen.getByRole("button", { name: "Live" }).getAttribute("aria-pressed"),
    ).toBe("true");
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

  it("shows the inspected step's screenshot when ?active= is set", () => {
    seedRun({ status: Status.Completed });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1&active=act_1`);

    const shot = screen.getByTestId("hero-screenshot");
    expect(JSON.parse(shot.getAttribute("data-selection") ?? "{}")).toEqual({
      kind: "action",
      artifactId: "art_1",
      stepId: "step_1",
      actionOrder: 0,
    });
    expect(screen.getByText(/Inspecting ·/)).toBeTruthy();
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
    expect(screen.getByRole("button", { name: "Stop recording" })).toBeTruthy();
  });

  it("the header Stop button requests the same finish path as the drafts panel", () => {
    seedRun({ status: Status.Completed });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    useRecordingStore.setState({ isRecording: true });
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    fireEvent.click(screen.getByRole("button", { name: "Stop recording" }));
    expect(useRecordingStore.getState().finishRequested).toBe(true);
  });

  it("a pinned Recording view without a recording shows the empty state", () => {
    seedRun({ status: Status.Completed });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    fireEvent.click(screen.getByRole("button", { name: "Recording" }));
    expect(screen.getByText("No recording for this run")).toBeTruthy();
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
  it("switches views from the header pills", () => {
    seedRun({ status: Status.Completed, recordingUrl: "https://r.test/1.mp4" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    expect(screen.getByTestId("hero-recording")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Live" }));
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

    fireEvent.click(screen.getByRole("button", { name: "Live" }));
    expect(screen.getByTestId("browser-pane-stream-slot")).toBeTruthy();

    // A timeline click pins the frame (even the already-selected one); the
    // pane hands the view back to the machine, which lands on Screenshots.
    act(() => {
      useRunViewStore.getState().pinFrame("act_1");
    });
    expect(screen.getByTestId("hero-screenshot")).toBeTruthy();
  });

  it("disables debug-browser actions while the run's own stream is shown", () => {
    seedRun({ status: Status.Running, browserSessionId: "pbs_run" });
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(`${STUDIO_PATH}&wr=wr_1`);

    expect(screen.getByTestId("run-live-stream")).toBeTruthy();
    for (const name of [
      "Reconnect browser stream",
      "Open browser in new tab",
      "Turn off browser",
    ]) {
      expect((screen.getByLabelText(name) as HTMLButtonElement).disabled).toBe(
        true,
      );
    }
  });

  it("keeps debug-browser actions enabled on the live debug stream", () => {
    mocks.debugSession = { browser_session_id: "pbs_test" };
    renderBrowserPane(STUDIO_PATH);

    for (const name of [
      "Reconnect browser stream",
      "Open browser in new tab",
      "Turn off browser",
    ]) {
      expect((screen.getByLabelText(name) as HTMLButtonElement).disabled).toBe(
        false,
      );
    }
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
      screen.getByRole("button", { name: "Live" }).getAttribute("aria-pressed"),
    ).toBe("false");
  });
});

describe("stream-mode badge dev gating", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("shows the transport badge in dev builds (vitest runs as dev)", () => {
    renderBrowserPane(STUDIO_PATH);
    expect(screen.queryByTestId("stream-mode-badge")).not.toBeNull();
  });

  it("hides the transport badge outside dev builds", () => {
    vi.stubEnv("DEV", false);
    renderBrowserPane(STUDIO_PATH);
    expect(screen.queryByTestId("stream-mode-badge")).toBeNull();
  });
});
