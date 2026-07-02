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

describe("StudioBrowserStream block-run take-control gating", () => {
  it("locks take-control while a block run executes in the debug session", () => {
    mockWorkflowRun(Status.Running, "pbs_test");
    renderStudioBrowserStream(BLOCK_RUN_OPEN_PATH);

    expect(controlButtonsAttr("emit vnc frame")).toBe("no");
    expect(screen.getByRole("status").textContent).toContain(
      "Skyvern is running this block",
    );
  });

  it("locks the CDP stream the same way (input socket keys off the same prop)", () => {
    runtimeConfigMock.browserStreamingMode = "cdp";
    mockWorkflowRun(Status.Running, "pbs_test");
    renderStudioBrowserStream(BLOCK_RUN_OPEN_PATH);

    expect(controlButtonsAttr("emit cdp activity")).toBe("no");
    expect(screen.getByRole("status").textContent).toContain(
      "Skyvern is running this block",
    );
  });

  it("re-enables take-control once the block run finalizes", () => {
    mockWorkflowRun(Status.Completed, "pbs_test");
    renderStudioBrowserStream(BLOCK_RUN_OPEN_PATH);

    expect(controlButtonsAttr("emit vnc frame")).toBe("yes");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("releases the lock while the block run is paused (may need human input)", () => {
    mockWorkflowRun(Status.Paused, "pbs_test");
    renderStudioBrowserStream(BLOCK_RUN_OPEN_PATH);

    expect(controlButtonsAttr("emit vnc frame")).toBe("yes");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("ignores a block run executing in a different browser session", () => {
    mockWorkflowRun(Status.Running, "pbs_other");
    renderStudioBrowserStream(BLOCK_RUN_OPEN_PATH);

    expect(controlButtonsAttr("emit vnc frame")).toBe("yes");
  });

  it("ignores a full (non-block) run even when its session matches", () => {
    mockWorkflowRun(Status.Running, "pbs_test");
    renderStudioBrowserStream(`${BROWSER_OPEN_PATH}&wr=run_1`);

    expect(controlButtonsAttr("emit vnc frame")).toBe("yes");
  });

  it("shows no lock pill while the Browser pane is closed", () => {
    mockWorkflowRun(Status.Running, "pbs_test");
    renderStudioBrowserStream(BLOCK_RUN_CLOSED_PATH);

    expect(controlButtonsAttr("emit vnc frame")).toBe("no");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("defers to an active recording instead of yanking its control", () => {
    useRecordingStore.setState({ isRecording: true });
    mockWorkflowRun(Status.Running, "pbs_test");
    renderStudioBrowserStream(BLOCK_RUN_OPEN_PATH);

    expect(controlButtonsAttr("emit vnc frame")).toBe("yes");
    expect(screen.queryByRole("status")).toBeNull();
  });
});
