import { StrictMode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserStream } from "./BrowserStream";
import { BrowserSession } from "@/routes/browserSessions/BrowserSession";

const mocks = vi.hoisted(() => {
  type RfbListener = (event: { detail?: unknown }) => void;

  const rfbInstances: Array<{
    clipboardPasteFrom: ReturnType<typeof vi.fn>;
    sendKey: ReturnType<typeof vi.fn>;
    disconnect: ReturnType<typeof vi.fn>;
    _framebufferUpdate: () => boolean;
  }> = [];
  const autoConnect = { value: true };
  const apiGet = vi.fn(async () => ({
    data: {
      browser_session_id: "pbs_test",
      status: "running",
      browser_address: "ws://browser.test",
      started_at: "2026-01-01T00:00:00Z",
      completed_at: null,
      stream_transport: "vnc",
      vnc_streaming_supported: true,
    },
  }));

  class MockRFB {
    scaleViewport = false;
    clipboardPasteFrom = vi.fn();
    sendKey = vi.fn();
    disconnect = vi.fn();
    _framebufferUpdate = vi.fn(() => true);

    private listeners: Record<string, RfbListener[]> = {};

    constructor(target: HTMLElement) {
      rfbInstances.push(this);
      target.appendChild(document.createElement("canvas"));
      if (autoConnect.value) {
        queueMicrotask(() => this.emit("connect"));
      }
    }

    addEventListener(type: string, listener: RfbListener) {
      this.listeners[type] = [...(this.listeners[type] ?? []), listener];
    }

    removeEventListener(type: string, listener: RfbListener) {
      this.listeners[type] = (this.listeners[type] ?? []).filter(
        (candidate) => candidate !== listener,
      );
    }

    private emit(type: string, detail?: unknown) {
      for (const listener of this.listeners[type] ?? []) {
        listener({ detail });
      }
    }
  }

  class MockWebSocket {
    onopen: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onclose: ((event: CloseEvent) => void) | null = null;
    send = vi.fn();
    close = vi.fn();

    constructor() {
      queueMicrotask(() => this.onopen?.(new Event("open")));
    }
  }

  const settingsStore = {
    setBrowserSessionId: vi.fn(),
    setIsUsingABrowser: vi.fn(),
  };

  const recordingStore = {
    add: vi.fn(),
    addScreenshot: vi.fn(),
    applyInterpretationUpdate: vi.fn(),
    compressedChunks: [],
    draftEditDepth: 0,
    getEventCount: vi.fn(() => 0),
    getSecondsRecording: vi.fn(() => 0),
    isRecording: false,
    manualCapturePaused: false,
    pendingEvents: [],
    reset: vi.fn(),
    setIsRecording: vi.fn(),
  };

  return {
    MockRFB,
    MockWebSocket,
    apiGet,
    autoConnect,
    rfbInstances,
    recordingStore,
    settingsStore,
    toast: vi.fn(),
  };
});

vi.mock("@novnc/novnc/lib/rfb.js", () => ({
  default: mocks.MockRFB,
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({ get: mocks.apiGet })),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => async () => null,
}));

vi.mock("@/hooks/useRuntimeConfig", () => ({
  resolveStreamTransport: (
    globalMode: string,
    sessionTransport: string | null | undefined,
  ) => sessionTransport ?? globalMode,
  useBrowserStreamingMode: () => ({ browserStreamingMode: "vnc" }),
}));

vi.mock(
  "@/routes/browserSessions/hooks/useCloseBrowserSessionMutation",
  () => ({
    useCloseBrowserSessionMutation: () => ({
      isPending: false,
      mutate: vi.fn(),
    }),
  }),
);
vi.mock(
  "@/routes/browserProfiles/hooks/useBackgroundBrowserProfileCreate",
  () => ({
    useBackgroundBrowserProfileCreate: () => ({
      startBackgroundCreate: vi.fn(),
    }),
  }),
);
vi.mock("@/routes/workflows/editor/Workspace", () => ({
  CopyText: () => null,
}));
vi.mock("@/components/ui/toaster", () => ({ Toaster: () => null }));
vi.mock("@/routes/browserProfiles/SaveSessionAsBrowserProfileDialog", () => ({
  SaveSessionAsBrowserProfileDialog: () => null,
}));
vi.mock("@/routes/browserSessions/BrowserSessionDownloads", () => ({
  BrowserSessionDownloads: () => null,
}));
vi.mock("@/routes/browserSessions/BrowserSessionOccupiedBy", () => ({
  BrowserSessionOccupiedBy: () => null,
}));
vi.mock("@/routes/browserSessions/BrowserSessionVideo", () => ({
  BrowserSessionVideo: () => null,
}));
vi.mock("@/routes/browserSessions/BrowserSessionTimeline", () => ({
  BrowserSessionTimeline: () => null,
}));
vi.mock("@/routes/browserSessions/BrowserSessionWorkflowRuns", () => ({
  BrowserSessionWorkflowRuns: () => null,
}));
vi.mock("@/routes/browserSessions/BrowserSessionStream", () => ({
  BrowserSessionStream: ({ forceCdp }: { forceCdp?: boolean }) => (
    <div
      data-force-cdp={forceCdp ? "true" : "false"}
      data-testid="cdp-screencast"
    />
  ),
}));

vi.mock("@/store/useClientIdStore", () => ({
  useClientIdStore: (selector: (state: { clientId: string }) => unknown) =>
    selector({ clientId: "client-test" }),
}));

vi.mock("@/store/SettingsStore", () => ({
  useSettingsStore: () => mocks.settingsStore,
}));

vi.mock("@/components/ui/use-toast", () => ({
  toast: mocks.toast,
}));

vi.mock("@/store/useRecordingStore", () => {
  // Honor the selector: BrowserStream reads slices (e.g. state.isRecording) via
  // useRecordingStore(selector). Ignoring the selector and returning the whole
  // store makes primitive selectors yield the store object instead of the field
  // value — truthy where a boolean was expected — spuriously rendering the
  // recording UI.
  const useRecordingStore = (
    selector?: (state: typeof mocks.recordingStore) => unknown,
  ) => (selector ? selector(mocks.recordingStore) : mocks.recordingStore);
  // Also read imperatively (reset/addScreenshot/etc.) via getState().
  useRecordingStore.getState = () => mocks.recordingStore;
  return {
    useRecordingStore,
    countVisibleDraftSteps: (steps: Array<unknown> = []) => steps.length,
  };
});

function renderBrowserStream(props: { onActivity?: () => void } = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <BrowserStream
        browserSessionId="pbs_test"
        interactive={false}
        showControlButtons={true}
        onActivity={props.onActivity}
      />
    </QueryClientProvider>,
  );
}

function renderWithRecordingReset(
  resetRecordingOnUnmount: boolean | undefined,
  { strict = false }: { strict?: boolean } = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const tree = (
    <QueryClientProvider client={queryClient}>
      <BrowserStream
        browserSessionId="pbs_test"
        interactive={false}
        showControlButtons={true}
        resetRecordingOnUnmount={resetRecordingOnUnmount}
      />
    </QueryClientProvider>
  );
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree);
}

function renderBrowserSession() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/browser-sessions/pbs_test/stream"]}>
        <Routes>
          <Route
            path="/browser-sessions/:browserSessionId/*"
            element={<BrowserSession />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("BrowserStream", () => {
  beforeEach(() => {
    mocks.autoConnect.value = true;
    Object.defineProperty(globalThis, "WebSocket", {
      configurable: true,
      value: mocks.MockWebSocket,
    });
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: mocks.MockWebSocket,
    });
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        readText: vi.fn(async () => "https://example.test"),
      },
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.clearAllMocks();
    mocks.rfbInstances.length = 0;
  });

  it("releases and restores held left Cmd around VNC paste", async () => {
    const { container } = renderBrowserStream();
    const takeControlButton = await screen.findByRole(
      "button",
      { name: /take control/i },
      { timeout: 10000 },
    );
    const stream = container.querySelector(".browser-stream");
    const canvas = container.querySelector("canvas");

    expect(stream).toBeInstanceOf(HTMLElement);
    expect(mocks.rfbInstances).toHaveLength(1);

    fireEvent.keyDown(stream!, { key: "v", metaKey: true });
    expect(mocks.rfbInstances[0]?.clipboardPasteFrom).not.toHaveBeenCalled();

    fireEvent.click(takeControlButton);
    fireEvent.keyDown(canvas!, { key: "Meta", code: "MetaLeft" });
    fireEvent.keyDown(stream!, { key: "v", metaKey: true });

    await waitFor(() => {
      expect(mocks.rfbInstances[0]?.clipboardPasteFrom).toHaveBeenCalledWith(
        "https://example.test",
      );
    });
    await waitFor(() => {
      expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenCalledTimes(6);
    });
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      1,
      0xffe9,
      "MetaLeft",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      2,
      0xffe3,
      "ControlLeft",
      true,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      3,
      0x0076,
      "KeyV",
      true,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      4,
      0x0076,
      "KeyV",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      5,
      0xffe3,
      "ControlLeft",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      6,
      0xffe9,
      "MetaLeft",
      true,
    );
  });

  it("clears a held Cmd side when Meta is released elsewhere in the window", async () => {
    const { container } = renderBrowserStream();
    const takeControlButton = await screen.findByRole(
      "button",
      { name: /take control/i },
      { timeout: 10000 },
    );
    const stream = container.querySelector(".browser-stream");
    const canvas = container.querySelector("canvas");

    expect(stream).toBeInstanceOf(HTMLElement);
    expect(mocks.rfbInstances).toHaveLength(1);

    fireEvent.click(takeControlButton);
    fireEvent.keyDown(canvas!, { key: "Meta", code: "MetaLeft" });
    fireEvent.keyUp(window, { key: "Meta", code: "MetaLeft" });
    fireEvent.keyDown(stream!, { key: "v", metaKey: true });

    await waitFor(() => {
      expect(mocks.rfbInstances[0]?.clipboardPasteFrom).toHaveBeenCalledWith(
        "https://example.test",
      );
    });
    await waitFor(() => {
      expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenCalledTimes(8);
    });
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      1,
      0xffe9,
      "AltLeft",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      2,
      0xffea,
      "AltRight",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      3,
      0xffeb,
      "MetaLeft",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      4,
      0xffec,
      "MetaRight",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      5,
      0xffe3,
      "ControlLeft",
      true,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      6,
      0x0076,
      "KeyV",
      true,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      7,
      0x0076,
      "KeyV",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      8,
      0xffe3,
      "ControlLeft",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).not.toHaveBeenCalledWith(
      0xffe9,
      "MetaLeft",
      true,
    );
  });

  it("does not restore a Cmd side whose keydown noVNC never received", async () => {
    const { container } = renderBrowserStream();
    const takeControlButton = await screen.findByRole(
      "button",
      { name: /take control/i },
      { timeout: 10000 },
    );
    const stream = container.querySelector(".browser-stream");

    expect(stream).toBeInstanceOf(HTMLElement);
    expect(mocks.rfbInstances).toHaveLength(1);

    fireEvent.click(takeControlButton);
    fireEvent.keyDown(stream!, { key: "Meta", code: "MetaLeft" });
    fireEvent.keyDown(stream!, { key: "v", metaKey: true });

    await waitFor(() => {
      expect(mocks.rfbInstances[0]?.clipboardPasteFrom).toHaveBeenCalledWith(
        "https://example.test",
      );
    });
    await waitFor(() => {
      expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenCalledTimes(8);
    });
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      1,
      0xffe9,
      "AltLeft",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      2,
      0xffea,
      "AltRight",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      3,
      0xffeb,
      "MetaLeft",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      4,
      0xffec,
      "MetaRight",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      5,
      0xffe3,
      "ControlLeft",
      true,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      6,
      0x0076,
      "KeyV",
      true,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      7,
      0x0076,
      "KeyV",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenNthCalledWith(
      8,
      0xffe3,
      "ControlLeft",
      false,
    );
    expect(mocks.rfbInstances[0]?.sendKey).not.toHaveBeenCalledWith(
      0xffe9,
      expect.any(String),
      true,
    );
    expect(mocks.rfbInstances[0]?.sendKey).not.toHaveBeenCalledWith(
      0xffea,
      expect.any(String),
      true,
    );
    expect(mocks.rfbInstances[0]?.sendKey).not.toHaveBeenCalledWith(
      0xffeb,
      expect.any(String),
      true,
    );
    expect(mocks.rfbInstances[0]?.sendKey).not.toHaveBeenCalledWith(
      0xffec,
      expect.any(String),
      true,
    );
  });

  it("shows a destructive toast when browser clipboard access fails", async () => {
    const error = new Error("denied");
    vi.mocked(navigator.clipboard.readText).mockRejectedValueOnce(error);
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    const { container } = renderBrowserStream();
    const takeControlButton = await screen.findByRole(
      "button",
      { name: /take control/i },
      { timeout: 10000 },
    );
    const stream = container.querySelector(".browser-stream");

    fireEvent.click(takeControlButton);
    fireEvent.keyDown(stream!, { key: "v", metaKey: true });

    await waitFor(() => {
      expect(mocks.toast).toHaveBeenCalledWith({
        title: "Paste failed",
        description:
          "Skyvern couldn't read your clipboard. Allow clipboard access for this site and try again.",
        variant: "destructive",
      });
    });
    expect(mocks.toast).toHaveBeenCalledTimes(1);
    expect(mocks.rfbInstances[0]?.clipboardPasteFrom).not.toHaveBeenCalled();
    expect(mocks.rfbInstances[0]?.sendKey).not.toHaveBeenCalled();
    expect(consoleError).toHaveBeenCalledTimes(1);
  });

  it("notifies activity after a VNC framebuffer update completes", async () => {
    const onActivity = vi.fn();

    renderBrowserStream({ onActivity });

    await waitFor(() => {
      expect(mocks.rfbInstances).toHaveLength(1);
    });

    mocks.rfbInstances[0]!._framebufferUpdate();

    expect(onActivity).toHaveBeenCalledTimes(1);
  });

  it("falls back to CDP when VNC disconnects before the handshake", async () => {
    mocks.autoConnect.value = false;

    renderBrowserSession();

    await waitFor(() => {
      expect(mocks.rfbInstances).toHaveLength(1);
    });

    const rfb = mocks.rfbInstances[0] as unknown as {
      emit: (type: string, detail?: unknown) => void;
    };
    rfb.emit("disconnect", { clean: false });

    await waitFor(() => {
      expect(
        screen.getByTestId("cdp-screencast").getAttribute("data-force-cdp"),
      ).toBe("true");
    });
  });

  it("shows a two-sentence message on an unclean VNC disconnect", async () => {
    renderBrowserStream();

    await waitFor(() => {
      expect(mocks.rfbInstances).toHaveLength(1);
    });

    const rfb = mocks.rfbInstances[0] as unknown as {
      emit: (type: string, detail?: unknown) => void;
    };
    await waitFor(() => {
      expect(mocks.settingsStore.setIsUsingABrowser).toHaveBeenCalledWith(true);
    });
    mocks.autoConnect.value = false;
    rfb.emit("disconnect", { clean: false });

    // The component reconnects after a disconnect, which flips isReady back
    // quickly -- assert everything about this one render in a single atomic
    // callback so a reconnect between separate awaits can't hide a failure.
    await waitFor(() => {
      expect(screen.getByText("The browser stream slipped away")).toBeTruthy();
      expect(
        screen.getByText(
          "Refresh the page or switch to local browser streaming.",
        ),
      ).toBeTruthy();
      expect(
        screen.queryByText(
          "The browser stream dropped before everything wrapped up.",
        ),
      ).toBeNull();
    });
  });

  describe("recording reset lifecycle", () => {
    it("resets the recording store on unmount by default", async () => {
      const { unmount } = renderWithRecordingReset(undefined);
      await waitFor(() => expect(mocks.rfbInstances).toHaveLength(1));

      expect(mocks.recordingStore.reset).not.toHaveBeenCalled();
      unmount();
      expect(mocks.recordingStore.reset).toHaveBeenCalledTimes(1);
    });

    it("does not reset the recording store on unmount when opted out", async () => {
      const { unmount } = renderWithRecordingReset(false);
      await waitFor(() => expect(mocks.rfbInstances).toHaveLength(1));

      unmount();
      expect(mocks.recordingStore.reset).not.toHaveBeenCalled();
    });

    // Pins that StrictMode's transient unmount really fires the cleanup in this
    // environment, so the opt-out test below cannot pass vacuously.
    it("runs the unmount cleanup on a StrictMode double-mount by default", async () => {
      renderWithRecordingReset(undefined, { strict: true });
      await waitFor(() =>
        expect(mocks.recordingStore.reset).toHaveBeenCalledTimes(1),
      );
    });

    // The local repro: StrictMode remounts the fresh VNC stream (mount -> unmount
    // -> mount) the instant recording starts. The transient unmount must not
    // clear the recording that just began.
    it("survives a StrictMode double-mount when opted out", async () => {
      renderWithRecordingReset(false, { strict: true });
      await waitFor(() =>
        expect(mocks.rfbInstances.length).toBeGreaterThanOrEqual(1),
      );

      expect(mocks.recordingStore.reset).not.toHaveBeenCalled();
    });
  });
});
