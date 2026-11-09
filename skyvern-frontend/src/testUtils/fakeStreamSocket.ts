import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { expect, vi } from "vitest";

class FakeStreamSocket {
  static readonly OPEN = 1;
  static instances: FakeStreamSocket[] = [];

  readonly readyState = FakeStreamSocket.OPEN;
  readonly send = vi.fn();
  close = vi.fn();
  readonly url: string;
  readonly protocols: string | string[] | undefined;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  private listeners: Record<string, Array<(event: unknown) => void>> = {};

  constructor(url: string, protocols?: string | string[]) {
    this.url = url;
    this.protocols = protocols;
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

function socketsAt(path: string): FakeStreamSocket[] {
  return FakeStreamSocket.instances.filter((socket) =>
    new URL(socket.url).pathname.includes(path),
  );
}

// Mirrors the screencast route for a session whose resolved transport is VNC:
// without force_cdp it closes 4001 use-vnc-streaming, with it it serves frames.
function answerAsVncSessionBackend(socket: FakeStreamSocket, frame: string) {
  if (new URL(socket.url).searchParams.get("force_cdp") === "true") {
    socket.emitStreamMessage({ status: "running", screenshot: frame });
  } else {
    socket.emit("close", { code: 4001, reason: "use-vnc-streaming" });
  }
}

const SCREENCAST_PATH = "/stream/browser_sessions/";

const renderedFrame = (frame: string) =>
  document.querySelector(`img[src="data:image/png;base64,${frame}"]`);

async function closeVncAndServeFrame(frame: string) {
  fireEvent.click(screen.getByTestId("vnc-stream"));
  await waitFor(() => expect(socketsAt(SCREENCAST_PATH)).toHaveLength(1));
  const socket = socketsAt(SCREENCAST_PATH)[0]!;
  act(() => answerAsVncSessionBackend(socket, frame));
  return socket;
}

async function expectForcedCdpFrame(
  socket: FakeStreamSocket,
  browserSessionId: string,
  frame: string,
) {
  const url = new URL(socket.url);
  expect(url.pathname).toBe(
    `/api/v1/stream/browser_sessions/${browserSessionId}`,
  );
  expect(url.search).toBe("?force_cdp=true");
  await waitFor(() => expect(renderedFrame(frame)).not.toBeNull());
  expect(screen.queryByText("Backend wants to use VNC streaming")).toBeNull();
  expect(socketsAt(SCREENCAST_PATH)).toHaveLength(1);
}

function expectUnforcedInputSocket(browserSessionId: string) {
  const inputSockets = socketsAt("/stream/cdp_input/");
  expect(inputSockets).toHaveLength(1);
  const url = new URL(inputSockets[0]!.url);
  expect(url.pathname).toBe(
    `/api/v1/stream/cdp_input/browser_session/${browserSessionId}`,
  );
  expect(url.searchParams.has("force_cdp")).toBe(false);
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

export {
  FakeStreamSocket,
  SCREENCAST_PATH,
  closeVncAndServeFrame,
  expectForcedCdpFrame,
  expectUnforcedInputSocket,
  socketsAt,
  stubAnimationFrame,
};
