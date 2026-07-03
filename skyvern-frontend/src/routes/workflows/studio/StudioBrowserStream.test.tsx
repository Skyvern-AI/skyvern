// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { StudioBrowserStream } from "./StudioBrowserStream";
import { useStudioPanes } from "./useStudioPanes";

const runtimeConfigMock = vi.hoisted(() => ({
  browserStreamingMode: "vnc",
}));

const workflowRunQueryMock = vi.hoisted(() => vi.fn());

vi.mock("../hooks/useDebugSessionQuery", () => ({
  useDebugSessionQuery: () => ({
    data: { browser_session_id: "pbs_test" },
  }),
}));

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: (options?: { workflowRunId?: string }) =>
    workflowRunQueryMock(options),
}));

vi.mock("../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: () => ({ data: undefined }),
}));

vi.mock("../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => ({ data: [], isPending: false }),
}));

vi.mock("@/hooks/useRuntimeConfig", () => ({
  useBrowserStreamingMode: () => ({
    browserStreamingMode: runtimeConfigMock.browserStreamingMode,
  }),
}));

vi.mock("@/components/BrowserStream", () => ({
  BrowserStream: ({
    onActivity,
    onReadyChange,
    showControlButtons,
  }: {
    onActivity?: () => void;
    onReadyChange?: (isReady: boolean, browserSessionId: string | null) => void;
    showControlButtons?: boolean;
  }) => (
    <div data-show-control-buttons={showControlButtons ? "yes" : "no"}>
      <button type="button" onClick={() => onReadyChange?.(true, "pbs_test")}>
        emit vnc ready
      </button>
      <button type="button" onClick={onActivity}>
        emit vnc frame
      </button>
    </div>
  ),
}));

vi.mock("@/routes/browserSessions/BrowserSessionStream", () => ({
  BrowserSessionStream: ({
    onActivity,
    onUrlChange,
    showControlButtons,
  }: {
    onActivity?: () => void;
    onUrlChange?: (url: string) => void;
    showControlButtons?: boolean;
  }) => (
    <div data-show-control-buttons={showControlButtons ? "yes" : "no"}>
      <button type="button" onClick={onActivity}>
        emit cdp activity
      </button>
      <button
        type="button"
        onClick={() => onUrlChange?.("https://example.test")}
      >
        emit url
      </button>
    </div>
  ),
}));

const initialBrowserState = useStudioBrowserStore.getState();
const initialRecordingState = useRecordingStore.getState();

// Drives a real pane-state URL write, so the effect chain under test is the
// same one a spine click goes through.
function OpenBrowserPaneButton() {
  const { openPane } = useStudioPanes();
  return (
    <button type="button" onClick={() => openPane("browser")}>
      open browser pane
    </button>
  );
}

// The browser pane's visibility comes from ?panes= in the URL.
function renderStudioBrowserStream(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route
          path="/workflows/:workflowPermanentId/studio"
          element={
            <>
              <StudioBrowserStream />
              <OpenBrowserPaneButton />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

const BROWSER_CLOSED_PATH = "/workflows/wpid_test/studio?panes=editor";
const BROWSER_OPEN_PATH = "/workflows/wpid_test/studio?panes=editor,browser";
const BLOCK_RUN_OPEN_PATH = `${BROWSER_OPEN_PATH}&wr=run_1&bl=Block%201`;
const BLOCK_RUN_CLOSED_PATH = `${BROWSER_CLOSED_PATH}&wr=run_1&bl=Block%201`;

function mockWorkflowRun(status: Status, browserSessionId: string | null) {
  workflowRunQueryMock.mockReturnValue({
    data: { status, browser_session_id: browserSessionId },
  });
}

function controlButtonsAttr(streamButtonName: string): string | null {
  return (
    screen
      .getByRole("button", { name: streamButtonName })
      .parentElement?.getAttribute("data-show-control-buttons") ?? null
  );
}

beforeEach(() => {
  runtimeConfigMock.browserStreamingMode = "vnc";
  useStudioBrowserStore.setState(initialBrowserState, true);
  useRecordingStore.setState(initialRecordingState, true);
  workflowRunQueryMock.mockReset();
  workflowRunQueryMock.mockReturnValue({ data: undefined });
});

describe("StudioBrowserStream browser activity notifications", () => {
  it("marks activity while the Browser pane is closed", () => {
    renderStudioBrowserStream(BROWSER_CLOSED_PATH);

    fireEvent.click(screen.getByRole("button", { name: "emit vnc frame" }));

    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(true);
  });

  it("marks VNC activity after the initial stream connection", () => {
    renderStudioBrowserStream(BROWSER_CLOSED_PATH);

    fireEvent.click(screen.getByRole("button", { name: "emit vnc ready" }));
    useStudioBrowserStore.getState().clearActivity();

    fireEvent.click(screen.getByRole("button", { name: "emit vnc frame" }));

    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(true);
  });

  it("clears activity when the Browser pane opens", async () => {
    renderStudioBrowserStream(BROWSER_CLOSED_PATH);

    fireEvent.click(screen.getByRole("button", { name: "emit vnc frame" }));
    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "open browser pane" }));

    await waitFor(() => {
      expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
    });
  });

  it("keeps browser activity cleared while the Browser pane is open", () => {
    renderStudioBrowserStream(BROWSER_OPEN_PATH);

    fireEvent.click(screen.getByRole("button", { name: "emit vnc frame" }));

    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
  });

  it("shows the stream control buttons only while the Browser pane is open", () => {
    const { unmount } = renderStudioBrowserStream(BROWSER_OPEN_PATH);
    expect(
      screen
        .getByRole("button", { name: "emit vnc frame" })
        .parentElement?.getAttribute("data-show-control-buttons"),
    ).toBe("yes");
    unmount();

    renderStudioBrowserStream(BROWSER_CLOSED_PATH);
    expect(
      screen
        .getByRole("button", { name: "emit vnc frame" })
        .parentElement?.getAttribute("data-show-control-buttons"),
    ).toBe("no");
  });

  it("marks CDP activity while the Browser pane is closed", () => {
    runtimeConfigMock.browserStreamingMode = "cdp";
    renderStudioBrowserStream(BROWSER_CLOSED_PATH);

    fireEvent.click(screen.getByRole("button", { name: "emit cdp activity" }));

    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(true);
  });

  it("keeps CDP activity cleared while the Browser pane is open", () => {
    runtimeConfigMock.browserStreamingMode = "cdp";
    renderStudioBrowserStream(BROWSER_OPEN_PATH);

    fireEvent.click(screen.getByRole("button", { name: "emit cdp activity" }));

    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
  });

  it("keeps the latest stream URL separate from unseen activity", () => {
    runtimeConfigMock.browserStreamingMode = "cdp";
    renderStudioBrowserStream(BROWSER_OPEN_PATH);

    fireEvent.click(screen.getByRole("button", { name: "emit url" }));

    expect(useStudioBrowserStore.getState().streamUrl).toBe(
      "https://example.test",
    );
  });
});

describe("StudioBrowserStream block-run co-drive", () => {
  it("keeps take-control available while a block run executes (co-drive)", () => {
    mockWorkflowRun(Status.Running, "pbs_test");
    renderStudioBrowserStream(BLOCK_RUN_OPEN_PATH);

    expect(controlButtonsAttr("emit vnc frame")).toBe("yes");
    expect(screen.getByRole("status").textContent).toContain(
      "Agent is running — you're sharing the browser",
    );
  });

  it("keeps the CDP stream controllable the same way", () => {
    runtimeConfigMock.browserStreamingMode = "cdp";
    mockWorkflowRun(Status.Running, "pbs_test");
    renderStudioBrowserStream(BLOCK_RUN_OPEN_PATH);

    expect(controlButtonsAttr("emit cdp activity")).toBe("yes");
    expect(screen.getByRole("status").textContent).toContain(
      "Agent is running — you're sharing the browser",
    );
  });

  it("drops the sharing pill once the block run finalizes", () => {
    mockWorkflowRun(Status.Completed, "pbs_test");
    renderStudioBrowserStream(BLOCK_RUN_OPEN_PATH);

    expect(controlButtonsAttr("emit vnc frame")).toBe("yes");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows no sharing pill while the block run is paused (needs human input)", () => {
    mockWorkflowRun(Status.Paused, "pbs_test");
    renderStudioBrowserStream(BLOCK_RUN_OPEN_PATH);

    expect(controlButtonsAttr("emit vnc frame")).toBe("yes");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("parks controls when the block run streams from a different session", () => {
    // The pane's live surface is the run's own stream; the debug singleton is
    // parked, so it must withdraw take-control (and show no pill).
    mockWorkflowRun(Status.Running, "pbs_other");
    renderStudioBrowserStream(BLOCK_RUN_OPEN_PATH);

    expect(controlButtonsAttr("emit vnc frame")).toBe("no");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("cedes control while the pane shows a replay view", () => {
    // ?active= pins a step -> Screenshots view; the singleton is parked, so
    // the control offer (and any held grab) is withdrawn.
    mockWorkflowRun(Status.Running, "pbs_test");
    renderStudioBrowserStream(`${BLOCK_RUN_OPEN_PATH}&active=act_1`);

    expect(controlButtonsAttr("emit vnc frame")).toBe("no");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("ignores a full (non-block) run even when its session matches", () => {
    mockWorkflowRun(Status.Running, "pbs_test");
    renderStudioBrowserStream(`${BROWSER_OPEN_PATH}&wr=run_1`);

    expect(controlButtonsAttr("emit vnc frame")).toBe("yes");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("keeps other surfaces view-only while the Browser pane is closed", () => {
    mockWorkflowRun(Status.Running, "pbs_test");
    renderStudioBrowserStream(BLOCK_RUN_CLOSED_PATH);

    expect(controlButtonsAttr("emit vnc frame")).toBe("no");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows no sharing pill during a recording (the recorder is driving)", () => {
    useRecordingStore.setState({ isRecording: true });
    mockWorkflowRun(Status.Running, "pbs_test");
    renderStudioBrowserStream(BLOCK_RUN_OPEN_PATH);

    expect(controlButtonsAttr("emit vnc frame")).toBe("yes");
    expect(screen.queryByRole("status")).toBeNull();
  });
});
