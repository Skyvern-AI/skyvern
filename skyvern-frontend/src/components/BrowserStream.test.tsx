import { StrictMode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserStream } from "./BrowserStream";

const mocks = vi.hoisted(() => {
  type RfbListener = (event: { detail?: unknown }) => void;

  const rfbInstances: Array<{
    clipboardPasteFrom: ReturnType<typeof vi.fn>;
    sendKey: ReturnType<typeof vi.fn>;
    disconnect: ReturnType<typeof vi.fn>;
    _framebufferUpdate: () => boolean;
  }> = [];
  const apiGet = vi.fn(async () => ({
    data: {
      browser_session_id: "pbs_test",
      status: "running",
      browser_address: "ws://browser.test",
      started_at: "2026-01-01T00:00:00Z",
      completed_at: null,
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
      queueMicrotask(() => this.emit("connect"));
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
    rfbInstances,
    recordingStore,
    settingsStore,
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

vi.mock("@/store/useClientIdStore", () => ({
  useClientIdStore: (selector: (state: { clientId: string }) => unknown) =>
    selector({ clientId: "client-test" }),
}));

vi.mock("@/store/SettingsStore", () => ({
  useSettingsStore: () => mocks.settingsStore,
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

describe("BrowserStream", () => {
  beforeEach(() => {
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
    vi.clearAllMocks();
    mocks.rfbInstances.length = 0;
  });

  it("supports VNC paste after taking control of a browser session stream", async () => {
    const { container } = renderBrowserStream();
    const takeControlButton = await screen.findByRole("button", {
      name: /take control/i,
    });
    const stream = container.querySelector(".browser-stream");

    expect(stream).toBeInstanceOf(HTMLElement);
    expect(mocks.rfbInstances).toHaveLength(1);

    fireEvent.keyDown(stream!, { ctrlKey: true, key: "v" });
    expect(mocks.rfbInstances[0]?.clipboardPasteFrom).not.toHaveBeenCalled();

    fireEvent.click(takeControlButton);
    fireEvent.keyDown(stream!, { ctrlKey: true, key: "v" });

    await waitFor(() => {
      expect(mocks.rfbInstances[0]?.clipboardPasteFrom).toHaveBeenCalledWith(
        "https://example.test",
      );
    });
    await waitFor(() => {
      expect(mocks.rfbInstances[0]?.sendKey).toHaveBeenCalledTimes(4);
    });
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
