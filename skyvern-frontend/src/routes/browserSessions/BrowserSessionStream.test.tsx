import { createHash, randomBytes } from "node:crypto";
import { createServer, type Server } from "node:http";
import { createConnection, type AddressInfo, type Socket } from "node:net";
import type { Duplex } from "node:stream";
import { StrictMode } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSettingsStore } from "@/store/SettingsStore";
import {
  STREAM_MAX_RECONNECT_DELAY_MS,
  isTerminalStreamStatus,
  shouldReconnectStream,
} from "@/routes/streaming/streamLifecycle";
import { BrowserSessionStream } from "./BrowserSessionStream";
import { diagnosticForStatus } from "./BrowserSessionStream.utils";

const env = vi.hoisted(() => ({
  wssBaseUrl: "ws://127.0.0.1:8000/api/v1",
}));

const cdpInputState = vi.hoisted(() => ({
  viewportWidth: 0,
  viewportHeight: 0,
  clipboardPasteEnabled: false,
  clipboardCopyEnabled: false,
  userIsControlling: false,
  inputReady: false,
  setUserIsControlling: undefined as ((value: boolean) => void) | undefined,
  onInput: undefined as (() => void) | undefined,
}));

const telemetry = vi.hoisted(() => ({
  captureRecordBrowser: vi.fn(),
}));

const api = vi.hoisted(() => ({
  get: vi.fn(async () => ({ data: {} })),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({ get: api.get }),
}));

vi.mock("@/util/env", () => ({
  get newWssBaseUrl() {
    return env.wssBaseUrl;
  },
  getCredentialParam: vi.fn(async () => "apikey=test"),
}));

vi.mock("@/hooks/useCredentialGetter", () => {
  // Stable identity: the stream effect keys on it, and a fresh function per
  // render would tear the socket down and clear the frame on every state change.
  const credentialGetter = async () => null;
  return { useCredentialGetter: () => credentialGetter };
});

vi.mock("@/util/recordBrowserTelemetry", () => ({
  captureRecordBrowser: telemetry.captureRecordBrowser,
}));

vi.mock("@/routes/streaming/useCdpInput", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/routes/streaming/useCdpInput")>();
  return {
    ...actual,
    useCdpInput: (options: Parameters<typeof actual.useCdpInput>[0]) => {
      const { viewportWidth, viewportHeight } = options;
      cdpInputState.viewportWidth = viewportWidth;
      cdpInputState.viewportHeight = viewportHeight;
      cdpInputState.clipboardPasteEnabled = Boolean(options.onClipboardPaste);
      cdpInputState.clipboardCopyEnabled = Boolean(options.onClipboardCopy);
      cdpInputState.onInput = options.onInput;
      const result = actual.useCdpInput(options);
      cdpInputState.userIsControlling = result.userIsControlling;
      cdpInputState.inputReady = result.inputReady;
      cdpInputState.setUserIsControlling = result.setUserIsControlling;
      return result;
    },
  };
});

vi.mock("@/routes/streaming/InteractiveStreamView", () => ({
  InteractiveStreamView: ({
    streamImgSrc,
    currentUrl,
    showControlButtons,
    userIsControlling,
  }: {
    streamImgSrc: string;
    currentUrl?: string;
    showControlButtons: boolean;
    userIsControlling: boolean;
  }) => (
    <>
      <div
        data-frame={streamImgSrc}
        data-url={currentUrl}
        data-testid="stream-frame"
      />
      {showControlButtons && userIsControlling && (
        <button type="button">stop controlling</button>
      )}
    </>
  ),
}));

class FakeStreamSocket {
  static readonly OPEN = 1;
  static instances: FakeStreamSocket[] = [];

  readonly readyState = FakeStreamSocket.OPEN;
  readonly send = vi.fn();
  close = vi.fn();
  readonly url: string;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  private listeners: Record<string, Array<(event: unknown) => void>> = {};

  constructor(url: string) {
    this.url = url;
    FakeStreamSocket.instances.push(this);
  }

  addEventListener(type: string, listener: (event: unknown) => void) {
    this.listeners[type] = [...(this.listeners[type] ?? []), listener];
  }

  removeEventListener() {}

  emit(type: string, event: unknown) {
    if (type === "open") {
      this.onopen?.(event as Event);
    } else if (type === "message") {
      this.onmessage?.(event as MessageEvent);
    } else if (type === "close") {
      this.onclose?.(event as CloseEvent);
    }
    for (const listener of this.listeners[type] ?? []) {
      listener(event);
    }
  }

  emitStreamMessage(message: Record<string, unknown>) {
    this.emit("message", { data: JSON.stringify(message) });
  }
}

function stubAnimationFrame(): void {
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn((callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(performance.now()), 0),
    ),
  );
  vi.stubGlobal(
    "cancelAnimationFrame",
    vi.fn((handle: number) => window.clearTimeout(handle)),
  );
}

class DisconnectingWebSocketServer {
  private readonly server: Server;
  private readonly sockets: Duplex[] = [];
  private readonly waiters: Array<() => void> = [];

  constructor() {
    this.server = createServer();
    this.server.on("upgrade", (request, socket) => {
      const key = request.headers["sec-websocket-key"];
      if (typeof key !== "string") {
        socket.destroy();
        return;
      }
      const accept = createHash("sha1")
        .update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
        .digest("base64");
      socket.write(
        `HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`,
      );
      this.sockets.push(socket);
      this.waiters.splice(0).forEach((resolve) => resolve());
    });
  }

  async start() {
    await new Promise<void>((resolve) =>
      this.server.listen(0, "127.0.0.1", resolve),
    );
    return (this.server.address() as AddressInfo).port;
  }

  async waitForConnections(count: number) {
    while (this.sockets.length < count) {
      await new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(
          () => reject(new Error(`Expected ${count} WebSocket connections`)),
          3000,
        );
        this.waiters.push(() => {
          clearTimeout(timeout);
          resolve();
        });
      });
    }
  }

  disconnectLatest() {
    this.sockets[this.sockets.length - 1]?.destroy();
  }

  async stop() {
    this.sockets.forEach((socket) => socket.destroy());
    await new Promise<void>((resolve) => this.server.close(() => resolve()));
  }
}

class TransportWebSocket {
  static readonly OPEN = 1;

  readyState = 0;
  private readonly listeners: Record<string, Array<(event: unknown) => void>> =
    {};
  private readonly socket: Socket;
  private closedByClient = false;
  private upgraded = false;

  constructor(rawUrl: string) {
    const url = new URL(rawUrl);
    this.socket = createConnection(
      { host: url.hostname, port: Number(url.port) },
      () => {
        const key = randomBytes(16).toString("base64");
        this.socket.write(
          `GET ${url.pathname}${url.search} HTTP/1.1\r\nHost: ${url.host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n\r\n`,
        );
      },
    );
    this.socket.on("data", (data) => {
      if (!this.upgraded && data.toString().startsWith("HTTP/1.1 101")) {
        this.upgraded = true;
        this.readyState = TransportWebSocket.OPEN;
        this.emit("open", {});
      }
    });
    this.socket.on("error", () => this.emit("error", {}));
    this.socket.on("close", () => {
      this.readyState = 3;
      this.emit("close", {
        code: this.closedByClient ? 1000 : 1006,
        reason: "",
      });
    });
  }

  addEventListener(type: string, listener: (event: unknown) => void) {
    this.listeners[type] = [...(this.listeners[type] ?? []), listener];
  }

  close() {
    this.closedByClient = true;
    this.socket.destroy();
  }

  private emit(type: string, event: unknown) {
    for (const listener of this.listeners[type] ?? []) {
      listener(event);
    }
  }
}

describe("isTerminalStreamStatus", () => {
  it("identifies terminal stream statuses", () => {
    expect(isTerminalStreamStatus("not_found")).toBe(true);
    expect(isTerminalStreamStatus("session_expired")).toBe(true);
    expect(isTerminalStreamStatus("running")).toBe(false);
  });
});

describe("diagnosticForStatus", () => {
  it("does not describe an expired session as a browser launch failure", () => {
    const expired = diagnosticForStatus("session_expired");
    const launchTimeout = diagnosticForStatus("timeout");

    expect(expired.title).not.toBe(launchTimeout.title);
    expect(expired.hint).not.toMatch(/BROWSER_STREAMING_MODE|backend logs/);
    expect(expired.pending).toBeFalsy();
  });

  it("reads a completed session as a success, not as a failure (SKY-13727)", () => {
    const completed = diagnosticForStatus("completed");

    expect(completed.tone).toBe("success");
    expect(completed.title).not.toBe(diagnosticForStatus("failed").title);
    expect(
      `${completed.title} ${completed.detail} ${completed.hint}`,
    ).not.toMatch(/wandered off|failed|error|lost|gone/i);
  });
});

describe("BrowserSessionStream terminal statuses", () => {
  beforeEach(() => {
    stubAnimationFrame();
    cdpInputState.setUserIsControlling = undefined;
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: FakeStreamSocket,
    });
    Object.defineProperty(globalThis, "WebSocket", {
      configurable: true,
      value: FakeStreamSocket,
    });
  });

  afterEach(() => {
    FakeStreamSocket.instances.length = 0;
    vi.clearAllMocks();
    vi.unstubAllGlobals();
  });

  it("replaces the last frame with the completed panel (SKY-13727)", async () => {
    render(<BrowserSessionStream browserSessionId="pbs_test" />);

    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(1));
    const socket = FakeStreamSocket.instances[0]!;

    act(() => {
      socket.emitStreamMessage({ status: "running", screenshot: "abc123" });
    });
    await waitFor(() =>
      expect(screen.getByTestId("stream-frame")).toBeTruthy(),
    );

    act(() => {
      socket.emitStreamMessage({ status: "completed" });
    });

    expect(screen.queryByTestId("stream-frame")).toBeNull();
    expect(screen.getByText("Browser session complete")).toBeTruthy();
  });

  it("opens no recording message socket when exfiltrate is omitted", async () => {
    render(<BrowserSessionStream browserSessionId="pbs_test" />);

    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(1));

    expect(FakeStreamSocket.instances[0]?.url).toContain(
      "/stream/browser_sessions/",
    );
    expect(
      FakeStreamSocket.instances.some((socket) =>
        socket.url.includes("/stream/messages/browser_session/"),
      ),
    ).toBe(false);
    expect(cdpInputState).toMatchObject({
      clipboardPasteEnabled: false,
      clipboardCopyEnabled: false,
    });
  });

  it("opens the recording message socket when exfiltrate is provided", async () => {
    render(
      <BrowserSessionStream browserSessionId="pbs_test" exfiltrate={false} />,
    );

    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(2));

    expect(
      FakeStreamSocket.instances.some((socket) =>
        socket.url.includes("/stream/messages/browser_session/pbs_test"),
      ),
    ).toBe(true);
    expect(cdpInputState).toMatchObject({
      clipboardPasteEnabled: false,
      clipboardCopyEnabled: false,
    });
  });

  it("enables recording clipboard interception only while the message socket is connected", async () => {
    const view = render(
      <BrowserSessionStream browserSessionId="pbs_test" exfiltrate={true} />,
    );

    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(2));
    const messageSocket = FakeStreamSocket.instances.find((socket) =>
      socket.url.includes("/stream/messages/browser_session/pbs_test"),
    );
    expect(messageSocket).toBeTruthy();
    expect(cdpInputState).toMatchObject({
      clipboardPasteEnabled: false,
      clipboardCopyEnabled: false,
    });

    act(() => messageSocket?.emit("open", new Event("open")));
    await waitFor(() =>
      expect(cdpInputState).toMatchObject({
        clipboardPasteEnabled: true,
        clipboardCopyEnabled: true,
      }),
    );

    act(() =>
      messageSocket?.emit("close", new CloseEvent("close", { code: 1006 })),
    );
    await waitFor(() =>
      expect(cdpInputState).toMatchObject({
        clipboardPasteEnabled: false,
        clipboardCopyEnabled: false,
      }),
    );
    view.unmount();
  });

  it("takes control for recording and only cedes a recording-owned grab", async () => {
    const view = render(
      <BrowserSessionStream
        browserSessionId="pbs_test"
        exfiltrate={false}
        interactive={true}
      />,
    );
    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(3));
    const inputSocket = FakeStreamSocket.instances.find((socket) =>
      socket.url.includes("/stream/cdp_input/browser_session/pbs_test"),
    );
    expect(inputSocket).toBeTruthy();
    inputSocket?.send.mockClear();

    view.rerender(
      <BrowserSessionStream
        browserSessionId="pbs_test"
        exfiltrate={true}
        interactive={true}
      />,
    );
    await waitFor(() =>
      expect(inputSocket?.send).toHaveBeenCalledWith(
        JSON.stringify({ kind: "take-control" }),
      ),
    );

    view.rerender(
      <BrowserSessionStream
        browserSessionId="pbs_test"
        exfiltrate={false}
        interactive={true}
      />,
    );
    await waitFor(() =>
      expect(inputSocket?.send).toHaveBeenCalledWith(
        JSON.stringify({ kind: "cede-control" }),
      ),
    );

    act(() => cdpInputState.setUserIsControlling?.(true));
    await waitFor(() =>
      expect(inputSocket?.send).toHaveBeenCalledWith(
        JSON.stringify({ kind: "take-control" }),
      ),
    );
    inputSocket?.send.mockClear();

    view.rerender(
      <BrowserSessionStream
        browserSessionId="pbs_test"
        exfiltrate={true}
        interactive={true}
      />,
    );
    view.rerender(
      <BrowserSessionStream
        browserSessionId="pbs_test"
        exfiltrate={false}
        interactive={true}
      />,
    );
    expect(inputSocket?.send).not.toHaveBeenCalledWith(
      JSON.stringify({ kind: "cede-control" }),
    );
    view.unmount();
  });

  it("drops recording control on input close and retakes it when the reconnected input is ready", async () => {
    vi.useFakeTimers();
    const view = render(
      <BrowserSessionStream
        browserSessionId="pbs_test"
        exfiltrate={true}
        interactive={true}
      />,
    );
    try {
      await act(async () => Promise.resolve());
      const firstInputSocket = FakeStreamSocket.instances.find((socket) =>
        socket.url.includes("/stream/cdp_input/browser_session/pbs_test"),
      );
      expect(firstInputSocket).toBeTruthy();
      act(() => {
        firstInputSocket?.emit("message", {
          data: JSON.stringify({ kind: "ready" }),
        });
      });
      await act(async () => Promise.resolve());
      expect(cdpInputState.userIsControlling).toBe(true);

      act(() => {
        firstInputSocket?.emit(
          "close",
          new CloseEvent("close", { code: 4411 }),
        );
      });
      await act(async () => Promise.resolve());
      expect(cdpInputState).toMatchObject({
        inputReady: false,
        userIsControlling: false,
      });

      await act(async () => vi.advanceTimersByTimeAsync(2000));
      await act(async () => Promise.resolve());
      const inputSockets = FakeStreamSocket.instances.filter((socket) =>
        socket.url.includes("/stream/cdp_input/browser_session/pbs_test"),
      );
      expect(inputSockets).toHaveLength(2);
      const reconnectedInputSocket = inputSockets[1]!;
      reconnectedInputSocket.send.mockClear();
      act(() => reconnectedInputSocket.emit("open", new Event("open")));
      expect(cdpInputState.userIsControlling).toBe(false);
      expect(reconnectedInputSocket.send).not.toHaveBeenCalledWith(
        JSON.stringify({ kind: "take-control" }),
      );

      act(() => {
        reconnectedInputSocket.emit("message", {
          data: JSON.stringify({ kind: "ready" }),
        });
      });
      await act(async () => Promise.resolve());
      expect(cdpInputState.userIsControlling).toBe(true);
      expect(reconnectedInputSocket.send).toHaveBeenCalledWith(
        JSON.stringify({ kind: "take-control" }),
      );
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("hides manual stop controlling while recording", async () => {
    const view = render(
      <BrowserSessionStream
        browserSessionId="pbs_test"
        exfiltrate={false}
        showControlButtons={true}
      />,
    );
    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(3));
    const streamSocket = FakeStreamSocket.instances.find((socket) =>
      socket.url.includes("/stream/browser_sessions/pbs_test"),
    );
    act(() => {
      streamSocket?.emitStreamMessage({
        status: "running",
        screenshot: "frame",
      });
    });
    await waitFor(() =>
      expect(screen.getByTestId("stream-frame")).toBeTruthy(),
    );
    act(() => cdpInputState.setUserIsControlling?.(true));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "stop controlling" }),
      ).toBeTruthy(),
    );

    view.rerender(
      <BrowserSessionStream
        browserSessionId="pbs_test"
        exfiltrate={true}
        showControlButtons={true}
      />,
    );

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "stop controlling" }),
      ).toBeNull(),
    );
    view.unmount();
  });

  it("reconnects the recording channel after it disconnects during recording", async () => {
    vi.useFakeTimers();
    const view = render(
      <BrowserSessionStream browserSessionId="pbs_test" exfiltrate={true} />,
    );
    try {
      await act(async () => Promise.resolve());
      const firstMessageSocket = FakeStreamSocket.instances.find((socket) =>
        socket.url.includes("/stream/messages/browser_session/pbs_test"),
      );
      expect(firstMessageSocket).toBeTruthy();
      act(() => firstMessageSocket?.emit("open", new Event("open")));

      act(() =>
        firstMessageSocket?.emit(
          "close",
          new CloseEvent("close", { code: 1006 }),
        ),
      );
      await act(async () => vi.advanceTimersByTimeAsync(1000));
      await act(async () => Promise.resolve());

      const messageSockets = FakeStreamSocket.instances.filter((socket) =>
        socket.url.includes("/stream/messages/browser_session/pbs_test"),
      );
      expect(messageSockets).toHaveLength(2);
      const reconnectedSocket = messageSockets[1]!;
      act(() => reconnectedSocket.emit("open", new Event("open")));
      expect(
        reconnectedSocket.send.mock.calls.map((call) =>
          JSON.parse(String(call[0])),
        ),
      ).toContainEqual(expect.objectContaining({ kind: "begin-exfiltration" }));

      act(() =>
        reconnectedSocket.emit(
          "close",
          new CloseEvent("close", { code: 1006 }),
        ),
      );
      await act(async () => vi.advanceTimersByTimeAsync(1000));
      await act(async () => Promise.resolve());
      expect(
        FakeStreamSocket.instances.filter((socket) =>
          socket.url.includes("/stream/messages/browser_session/pbs_test"),
        ),
      ).toHaveLength(3);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("reconnects a channel stranded by stopping before its retry", async () => {
    vi.useFakeTimers();
    const view = render(
      <BrowserSessionStream browserSessionId="pbs_test" exfiltrate={true} />,
    );
    try {
      await act(async () => Promise.resolve());
      const firstMessageSocket = FakeStreamSocket.instances.find((socket) =>
        socket.url.includes("/stream/messages/browser_session/pbs_test"),
      );
      expect(firstMessageSocket).toBeTruthy();
      act(() => firstMessageSocket?.emit("open", new Event("open")));
      act(() =>
        firstMessageSocket?.emit(
          "close",
          new CloseEvent("close", { code: 1006 }),
        ),
      );

      view.rerender(
        <BrowserSessionStream browserSessionId="pbs_test" exfiltrate={false} />,
      );
      await act(async () => vi.advanceTimersByTimeAsync(1000));
      expect(
        FakeStreamSocket.instances.filter((socket) =>
          socket.url.includes("/stream/messages/browser_session/pbs_test"),
        ),
      ).toHaveLength(1);

      view.rerender(
        <BrowserSessionStream browserSessionId="pbs_test" exfiltrate={true} />,
      );
      await act(async () => vi.advanceTimersByTimeAsync(1000));
      await act(async () => Promise.resolve());
      const messageSockets = FakeStreamSocket.instances.filter((socket) =>
        socket.url.includes("/stream/messages/browser_session/pbs_test"),
      );
      expect(messageSockets).toHaveLength(2);
      act(() => messageSockets[1]!.emit("open", new Event("open")));
      expect(
        messageSockets[1]!.send.mock.calls.map((call) =>
          JSON.parse(String(call[0])),
        ),
      ).toContainEqual(expect.objectContaining({ kind: "begin-exfiltration" }));
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("reports aggregate stream health once when recording ends", async () => {
    vi.useFakeTimers();
    const view = render(
      <StrictMode>
        <BrowserSessionStream browserSessionId="pbs_test" exfiltrate={true} />
      </StrictMode>,
    );
    try {
      await act(async () => Promise.resolve());
      const streamSocket = FakeStreamSocket.instances.find(
        (socket) =>
          socket.url.includes("/stream/browser_sessions/") &&
          !socket.close.mock.calls.length,
      );
      expect(streamSocket).toBeTruthy();

      act(() => {
        streamSocket?.emitStreamMessage({
          status: "running",
          screenshot: "frame-1",
        });
        streamSocket?.emitStreamMessage({
          status: "running",
          screenshot: "frame-2",
        });
        streamSocket?.emitStreamMessage({
          status: "running",
          screenshot: "frame-3",
        });
      });
      await act(async () => vi.advanceTimersByTimeAsync(30_000));

      act(() => {
        for (let frame = 0; frame < 6; frame += 1) {
          streamSocket?.emitStreamMessage({
            status: "running",
            screenshot: `frame-${frame + 4}`,
          });
        }
      });
      await act(async () => vi.advanceTimersByTimeAsync(30_000));

      expect(telemetry.captureRecordBrowser).not.toHaveBeenCalled();

      view.rerender(
        <StrictMode>
          <BrowserSessionStream
            browserSessionId="pbs_test"
            exfiltrate={false}
          />
        </StrictMode>,
      );
      await act(async () => vi.advanceTimersByTimeAsync(0));

      expect(telemetry.captureRecordBrowser).toHaveBeenCalledOnce();
      expect(telemetry.captureRecordBrowser).toHaveBeenCalledWith(
        "record_browser.cdp_stream_health",
        expect.objectContaining({
          sample_count: 2,
        }),
      );
      const properties = telemetry.captureRecordBrowser.mock.calls[0]?.[1];
      expect(properties?.fps_avg).toBeCloseTo(0.15);
      expect(properties?.fps_min).toBeCloseTo(0.1);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("shows the terminal panel even if a terminal status arrives with a frame", async () => {
    render(<BrowserSessionStream browserSessionId="pbs_test" />);

    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(1));

    act(() => {
      FakeStreamSocket.instances[0]!.emitStreamMessage({
        status: "completed",
        screenshot: "abc123",
      });
    });

    await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));

    expect(screen.queryByTestId("stream-frame")).toBeNull();
    expect(screen.getByText("Browser session complete")).toBeTruthy();
  });

  it("retires the prior socket when the displayed browser session changes", async () => {
    const { rerender } = render(
      <BrowserSessionStream browserSessionId="pbs_first" />,
    );

    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(1));
    const firstSocket = FakeStreamSocket.instances[0]!;

    rerender(<BrowserSessionStream browserSessionId="pbs_second" />);
    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(2));
    const secondSocket = FakeStreamSocket.instances[1]!;

    // The first socket can report its close after the second socket has already
    // become current. That stale callback must not orphan the second socket.
    act(() => {
      firstSocket.emit("close", { code: 1000, reason: "" });
    });

    rerender(<BrowserSessionStream browserSessionId="pbs_third" />);
    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(3));
    const thirdSocket = FakeStreamSocket.instances[2]!;

    expect(secondSocket.close).toHaveBeenCalledOnce();

    act(() => {
      thirdSocket.emitStreamMessage({
        browser_session_id: "pbs_third",
        status: "running",
        screenshot: "third-frame",
      });
      secondSocket.emitStreamMessage({
        browser_session_id: "pbs_second",
        status: "running",
        screenshot: "stale-second-frame",
      });
    });

    await waitFor(() =>
      expect(
        screen.getByTestId("stream-frame").getAttribute("data-frame"),
      ).toBe("third-frame"),
    );
  });

  it("ignores a frame tagged for another browser session", async () => {
    render(<BrowserSessionStream browserSessionId="pbs_current" />);

    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(1));
    const socket = FakeStreamSocket.instances[0]!;

    act(() => {
      socket.emitStreamMessage({
        browser_session_id: "pbs_current",
        status: "running",
        screenshot: "current-frame",
      });
      socket.emitStreamMessage({
        browser_session_id: "pbs_other",
        status: "running",
        screenshot: "other-frame",
      });
    });

    await waitFor(() =>
      expect(
        screen.getByTestId("stream-frame").getAttribute("data-frame"),
      ).toBe("current-frame"),
    );
  });

  it("applies a frame's metadata in the same commit as its pixels", async () => {
    render(<BrowserSessionStream browserSessionId="pbs_test" />);
    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(1));
    const socket = FakeStreamSocket.instances[0]!;

    act(() => {
      socket.emitStreamMessage({
        status: "running",
        screenshot: "first",
        url: "https://first.test/",
      });
    });
    await waitFor(() =>
      expect(
        screen.getByTestId("stream-frame").getAttribute("data-frame"),
      ).toBe("first"),
    );

    // A navigated frame: its url describes pixels that have not been committed yet,
    // so showing it now would label the previous screenshot with the next page.
    act(() => {
      socket.emitStreamMessage({
        status: "running",
        screenshot: "second",
        url: "https://second.test/",
      });
    });
    const midFlight = screen.getByTestId("stream-frame");
    expect(midFlight.getAttribute("data-frame")).toBe("first");
    expect(midFlight.getAttribute("data-url")).toBe("https://first.test/");

    await waitFor(() =>
      expect(
        screen.getByTestId("stream-frame").getAttribute("data-frame"),
      ).toBe("second"),
    );
    expect(screen.getByTestId("stream-frame").getAttribute("data-url")).toBe(
      "https://second.test/",
    );
  });

  it("does not remap input for metadata-only updates", async () => {
    render(<BrowserSessionStream browserSessionId="pbs_test" />);
    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(1));
    const socket = FakeStreamSocket.instances[0]!;

    act(() => {
      socket.emitStreamMessage({
        status: "running",
        screenshot: "first",
        url: "https://first.test/",
        viewport_width: 800,
        viewport_height: 600,
      });
    });
    await waitFor(() =>
      expect(
        screen.getByTestId("stream-frame").getAttribute("data-frame"),
      ).toBe("first"),
    );
    expect(cdpInputState).toMatchObject({
      viewportWidth: 800,
      viewportHeight: 600,
    });

    act(() => {
      socket.emitStreamMessage({
        status: "running",
        url: "https://next.test/",
        viewport_width: 1600,
        viewport_height: 1200,
      });
    });

    expect(screen.getByTestId("stream-frame").getAttribute("data-url")).toBe(
      "https://first.test/",
    );
    expect(cdpInputState).toMatchObject({
      viewportWidth: 800,
      viewportHeight: 600,
    });
  });

  it("coalesces frames that arrive within one animation frame to the latest one", async () => {
    const raf = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation((cb) => {
        setTimeout(() => cb(performance.now()), 0);
        return 1;
      });
    render(<BrowserSessionStream browserSessionId="pbs_test" />);
    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(1));
    const socket = FakeStreamSocket.instances[0]!;
    act(() => {
      socket.emitStreamMessage({
        status: "running",
        screenshot: "AAA",
        format: "jpeg",
      });
      socket.emitStreamMessage({
        status: "running",
        screenshot: "BBB",
        format: "jpeg",
      });
    });
    await waitFor(() =>
      expect(
        screen.getByTestId("stream-frame").getAttribute("data-frame"),
      ).toBe("BBB"),
    );
    expect(raf).toHaveBeenCalledTimes(1);
    raf.mockRestore();
  });
});

describe("BrowserSessionStream reconnect lifecycle", () => {
  beforeEach(() => {
    stubAnimationFrame();
    FakeStreamSocket.instances.length = 0;
    vi.clearAllMocks();
    useSettingsStore.getState().setIsUsingABrowser(false);
    useSettingsStore.getState().setBrowserSessionId(null);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("reconnects after a real transport disconnect", async () => {
    const server = new DisconnectingWebSocketServer();
    const port = await server.start();
    env.wssBaseUrl = `ws://127.0.0.1:${port}/api/v1`;
    vi.stubGlobal("WebSocket", TransportWebSocket);

    const view = render(
      <BrowserSessionStream browserSessionId="pbs_disconnect" />,
    );
    try {
      await server.waitForConnections(1);
      server.disconnectLatest();
      await server.waitForConnections(2);
    } finally {
      view.unmount();
      await server.stop();
    }
  });

  it("resets the retry budget on a frame and stops at the exhaustion boundary", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeStreamSocket);
    render(<BrowserSessionStream browserSessionId="pbs_budget" />);
    await act(async () => Promise.resolve());
    expect(FakeStreamSocket.instances).toHaveLength(1);

    act(() => {
      FakeStreamSocket.instances[FakeStreamSocket.instances.length - 1]!.emit(
        "close",
        {
          code: 1006,
          reason: "",
        },
      );
    });
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(FakeStreamSocket.instances).toHaveLength(2);

    act(() => {
      FakeStreamSocket.instances[
        FakeStreamSocket.instances.length - 1
      ]!.emitStreamMessage({
        status: "running",
        screenshot: "budget-reset-frame",
      });
    });

    for (let attempt = 0; attempt < 20; attempt += 1) {
      act(() => {
        FakeStreamSocket.instances[FakeStreamSocket.instances.length - 1]!.emit(
          "close",
          {
            code: 1006,
            reason: "",
          },
        );
      });
      // Retries back off, so the longest gap covers every attempt in the budget.
      await act(async () =>
        vi.advanceTimersByTimeAsync(STREAM_MAX_RECONNECT_DELAY_MS),
      );
    }
    expect(FakeStreamSocket.instances).toHaveLength(22);

    act(() => {
      FakeStreamSocket.instances[FakeStreamSocket.instances.length - 1]!.emit(
        "close",
        {
          code: 1006,
          reason: "",
        },
      );
    });
    await act(async () =>
      vi.advanceTimersByTimeAsync(STREAM_MAX_RECONNECT_DELAY_MS),
    );
    expect(FakeStreamSocket.instances).toHaveLength(22);
    expect(screen.queryByTestId("stream-frame")).toBeNull();
    expect(screen.getByText("Stream connection dropped")).toBeTruthy();
  });

  it("retires the stale frame and reconnects when the stream ends non-terminally (SKY-14617)", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeStreamSocket);
    render(<BrowserSessionStream browserSessionId="pbs_stale" />);
    await act(async () => Promise.resolve());
    const socket = FakeStreamSocket.instances[0]!;

    act(() => {
      socket.emitStreamMessage({
        status: "running",
        screenshot: "last-live-frame",
      });
    });
    act(() => {
      vi.advanceTimersToNextFrame();
    });
    expect(screen.getByTestId("stream-frame")).toBeTruthy();

    // What the backend sends once its screencast loop returns for a session that is
    // still alive: a bare, non-terminal status, then a clean close.
    act(() => {
      socket.emitStreamMessage({ status: "running" });
    });
    expect(screen.queryByTestId("stream-frame")).toBeNull();
    expect(
      screen.getByText("This live view has stopped updating"),
    ).toBeTruthy();

    act(() => {
      socket.emit("close", { code: 1000, reason: "" });
    });
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(FakeStreamSocket.instances).toHaveLength(2);
  });

  it("terminal status closes once, clears the frame and store, and never retries", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeStreamSocket);
    render(<BrowserSessionStream browserSessionId="pbs_terminal" />);
    await act(async () => Promise.resolve());
    const socket = FakeStreamSocket.instances[0]!;

    act(() => {
      socket.emitStreamMessage({
        status: "running",
        screenshot: "last-live-frame",
      });
    });
    act(() => {
      vi.advanceTimersToNextFrame();
    });
    expect(useSettingsStore.getState()).toMatchObject({
      isUsingABrowser: true,
      browserSessionId: "pbs_terminal",
    });

    act(() => {
      socket.emitStreamMessage({ status: "completed" });
      socket.emit("close", { code: 1006, reason: "" });
    });
    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(socket.close).toHaveBeenCalledOnce();
    expect(FakeStreamSocket.instances).toHaveLength(1);
    expect(screen.queryByTestId("stream-frame")).toBeNull();
    expect(useSettingsStore.getState()).toMatchObject({
      isUsingABrowser: false,
      browserSessionId: null,
    });
  });

  it("unmount closes the stream and cancels any retry", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeStreamSocket);
    const view = render(
      <BrowserSessionStream browserSessionId="pbs_unmount" />,
    );
    await act(async () => Promise.resolve());
    const socket = FakeStreamSocket.instances[0]!;

    view.unmount();
    act(() => {
      socket.emit("close", { code: 1006, reason: "" });
    });
    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(socket.close).toHaveBeenCalledOnce();
    expect(FakeStreamSocket.instances).toHaveLength(1);
  });

  it("the incompatible-transport close never retries", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeStreamSocket);
    render(<BrowserSessionStream browserSessionId="pbs_vnc" />);
    await act(async () => Promise.resolve());

    act(() => {
      FakeStreamSocket.instances[0]!.emit("close", {
        code: 4001,
        reason: "use-vnc-streaming",
      });
    });
    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(FakeStreamSocket.instances).toHaveLength(1);
  });
});

describe("shouldReconnectStream", () => {
  it("reconnects a clean server close, not only an abnormal one (SKY-14617)", () => {
    expect(
      shouldReconnectStream({
        closeCode: 1006,
        closeReason: "",
        streamFinished: false,
        reconnectAttempts: 0,
      }),
    ).toBe(true);
    // The server closes cleanly once its screencast loop ends, which for a live
    // entity used to mean no reconnect at all and a frozen frame on screen.
    expect(
      shouldReconnectStream({
        closeCode: 1000,
        closeReason: "",
        streamFinished: false,
        reconnectAttempts: 0,
      }),
    ).toBe(true);
  });

  it("does not reconnect a finished, transport-switched, or exhausted stream", () => {
    expect(
      shouldReconnectStream({
        closeCode: 1000,
        closeReason: "",
        streamFinished: true,
        reconnectAttempts: 0,
      }),
    ).toBe(false);
    expect(
      shouldReconnectStream({
        closeCode: 4001,
        closeReason: "use-vnc-streaming",
        streamFinished: false,
        reconnectAttempts: 0,
      }),
    ).toBe(false);
    expect(
      shouldReconnectStream({
        closeCode: 1006,
        closeReason: "",
        streamFinished: false,
        reconnectAttempts: 20,
      }),
    ).toBe(false);
  });
});
