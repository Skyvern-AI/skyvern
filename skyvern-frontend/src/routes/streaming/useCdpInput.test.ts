import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useCdpInput } from "./useCdpInput";

const mocks = vi.hoisted(() => ({
  credentialGetter: vi.fn(async () => null),
  getCredentialParam: vi.fn(async () => "token=Bearer+test-token"),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => mocks.credentialGetter,
}));

vi.mock("@/store/useClientIdStore", () => ({
  useClientIdStore: (selector: (state: { clientId: string }) => unknown) =>
    selector({ clientId: "client-test" }),
}));

vi.mock("@/util/env", () => ({
  getCredentialParam: mocks.getCredentialParam,
}));

type SocketListener = (event: Event) => void;

class MockWebSocket {
  static readonly OPEN = 1;
  static instances: MockWebSocket[] = [];

  readonly readyState = MockWebSocket.OPEN;
  readonly send = vi.fn();
  readonly close = vi.fn();

  private listeners: Record<string, SocketListener[]> = {};

  constructor() {
    MockWebSocket.instances.push(this);
  }

  addEventListener(type: string, listener: SocketListener) {
    this.listeners[type] = [...(this.listeners[type] ?? []), listener];
  }

  emitClose(code: number) {
    const event = new CloseEvent("close", { code });
    for (const listener of this.listeners.close ?? []) {
      listener(event);
    }
  }
}

async function renderInputHook() {
  renderHook(() =>
    useCdpInput({
      inputWsUrl: "wss://input.test",
      interactive: true,
      viewportWidth: 1280,
      viewportHeight: 720,
    }),
  );

  await act(async () => {
    await Promise.resolve();
  });
}

function closeLatestSocket(code: number) {
  const socket = MockWebSocket.instances[MockWebSocket.instances.length - 1];
  if (!socket) throw new Error("No WebSocket was constructed");

  act(() => {
    socket.emitClose(code);
  });
}

async function advanceReconnectDelay() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
}

describe("useCdpInput reconnects", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("constructs a new socket after a 4411 close", async () => {
    await renderInputHook();
    expect(MockWebSocket.instances).toHaveLength(1);

    closeLatestSocket(4411);
    await advanceReconnectDelay();

    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it("does not reconnect after a normal close", async () => {
    await renderInputHook();
    expect(MockWebSocket.instances).toHaveLength(1);

    closeLatestSocket(1000);
    await advanceReconnectDelay();

    expect(MockWebSocket.instances).toHaveLength(1);
  });

  // Uses 4410, not 4411: the cap only bites when the server never reaches "ready", which resets the
  // counter. 4410 is the setup-time close, so repeated failures without "ready" is its real shape.
  it("stops reconnecting after five attempts that never reach ready", async () => {
    await renderInputHook();

    for (let attempt = 0; attempt < 5; attempt += 1) {
      closeLatestSocket(4410);
      await advanceReconnectDelay();
    }
    expect(MockWebSocket.instances).toHaveLength(6);

    closeLatestSocket(4410);
    await advanceReconnectDelay();

    expect(MockWebSocket.instances).toHaveLength(6);
  });
});
