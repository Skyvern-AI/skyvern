// @vitest-environment jsdom

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useStudioShellStore } from "@/store/StudioShellStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

import { StudioBrowserStream } from "./StudioBrowserStream";

const runtimeConfigMock = vi.hoisted(() => ({
  browserStreamingMode: "vnc",
}));

vi.mock("../hooks/useDebugSessionQuery", () => ({
  useDebugSessionQuery: () => ({
    data: { browser_session_id: "pbs_test" },
  }),
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

function renderStudioBrowserStream() {
  return render(
    <MemoryRouter initialEntries={["/workflows/wpid_test/studio"]}>
      <Routes>
        <Route
          path="/workflows/:workflowPermanentId/studio"
          element={<StudioBrowserStream />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  runtimeConfigMock.browserStreamingMode = "vnc";
  useStudioBrowserStore.setState(initialBrowserState, true);
  useStudioShellStore.getState().reset();
});

describe("StudioBrowserStream browser activity notifications", () => {
  it("marks activity while the user is away from the Browser tab", () => {
    useStudioShellStore.getState().setTab("editor");
    renderStudioBrowserStream();

    fireEvent.click(screen.getByRole("button", { name: "emit vnc frame" }));

    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(true);
  });

  it("marks VNC activity after the initial stream connection", () => {
    useStudioShellStore.getState().setTab("editor");
    renderStudioBrowserStream();

    fireEvent.click(screen.getByRole("button", { name: "emit vnc ready" }));
    act(() => {
      useStudioBrowserStore.getState().clearActivity();
    });

    fireEvent.click(screen.getByRole("button", { name: "emit vnc frame" }));

    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(true);
  });

  it("clears activity when the Browser tab is active", async () => {
    useStudioShellStore.getState().setTab("editor");
    renderStudioBrowserStream();

    fireEvent.click(screen.getByRole("button", { name: "emit vnc frame" }));
    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(true);

    act(() => {
      useStudioShellStore.getState().setTab("browser");
    });

    await waitFor(() => {
      expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
    });
  });

  it("keeps browser activity cleared while the Browser tab is visible", () => {
    useStudioShellStore.getState().setTab("browser");
    renderStudioBrowserStream();

    fireEvent.click(screen.getByRole("button", { name: "emit vnc frame" }));

    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
  });

  it("marks CDP activity while the user is away from the Browser tab", () => {
    runtimeConfigMock.browserStreamingMode = "cdp";
    useStudioShellStore.getState().setTab("editor");
    renderStudioBrowserStream();

    fireEvent.click(screen.getByRole("button", { name: "emit cdp activity" }));

    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(true);
  });

  it("keeps CDP activity cleared while the Browser tab is visible", () => {
    runtimeConfigMock.browserStreamingMode = "cdp";
    useStudioShellStore.getState().setTab("browser");
    renderStudioBrowserStream();

    fireEvent.click(screen.getByRole("button", { name: "emit cdp activity" }));

    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
  });

  it("keeps the latest stream URL separate from unseen activity", () => {
    runtimeConfigMock.browserStreamingMode = "cdp";
    renderStudioBrowserStream();

    fireEvent.click(screen.getByRole("button", { name: "emit url" }));

    expect(useStudioBrowserStore.getState().streamUrl).toBe(
      "https://example.test",
    );
  });
});
