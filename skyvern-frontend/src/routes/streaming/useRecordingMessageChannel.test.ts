import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRecordingMessageChannel } from "./useRecordingMessageChannel";

const mocks = vi.hoisted(() => ({
  getWebSocketParams: vi.fn(async () => "client_id=client-test"),
}));

vi.mock("./webSocketParams", () => ({
  useWebSocketParams: () => mocks.getWebSocketParams,
}));

vi.mock("@/util/env", () => ({
  newWssBaseUrl: "wss://api.test",
}));

class MockWebSocket {
  static readonly OPEN = 1;
  static instances: MockWebSocket[] = [];

  readonly readyState = MockWebSocket.OPEN;
  readonly send = vi.fn();
  readonly close = vi.fn();
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  constructor(readonly url: string) {
    MockWebSocket.instances.push(this);
  }

  emitOpen() {
    this.onopen?.(new Event("open"));
  }

  emitMessage(message: unknown) {
    this.onmessage?.(
      new MessageEvent("message", { data: JSON.stringify(message) }),
    );
  }

  emitClose(code = 1006) {
    this.onclose?.(new CloseEvent("close", { code }));
  }
}

async function openSocket() {
  await act(async () => {
    await Promise.resolve();
  });
  const socket = MockWebSocket.instances[MockWebSocket.instances.length - 1];
  if (!socket) {
    throw new Error("No WebSocket was constructed");
  }
  act(() => socket.emitOpen());
  return socket;
}

describe("useRecordingMessageChannel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends commands once the socket is open", async () => {
    const { result } = renderHook(() =>
      useRecordingMessageChannel({
        browserSessionId: "pbs-1",
        enabled: true,
        clipboard: "none",
      }),
    );

    expect(result.current.sendCommand({ kind: "take-control" })).toBe(false);
    const socket = await openSocket();

    let sent = false;
    act(() => {
      sent = result.current.sendCommand({ kind: "take-control" });
    });
    expect(sent).toBe(true);
    expect(socket.send).toHaveBeenCalledWith(
      JSON.stringify({ kind: "take-control" }),
    );
  });

  it("does not construct a socket after credential loading is cancelled", async () => {
    let resolveParams: ((params: string) => void) | undefined;
    mocks.getWebSocketParams.mockReturnValueOnce(
      new Promise<string>((resolve) => {
        resolveParams = resolve;
      }),
    );
    const { unmount } = renderHook(() =>
      useRecordingMessageChannel({
        browserSessionId: "pbs-1",
        enabled: true,
        clipboard: "none",
      }),
    );

    unmount();
    await act(async () => {
      resolveParams?.("client_id=late");
      await Promise.resolve();
    });

    expect(MockWebSocket.instances).toEqual([]);
  });

  it("reports disconnected when credential setup fails", async () => {
    const onConnectionChange = vi.fn();
    mocks.getWebSocketParams.mockRejectedValueOnce(
      new Error("credential lookup failed"),
    );

    renderHook(() =>
      useRecordingMessageChannel({
        browserSessionId: "pbs-1",
        enabled: true,
        clipboard: "none",
        onConnectionChange,
      }),
    );
    await act(async () => {
      await Promise.resolve();
    });

    expect(MockWebSocket.instances).toEqual([]);
    expect(onConnectionChange).toHaveBeenCalledWith(
      false,
      expect.objectContaining({ code: 1011 }),
    );
  });
});
