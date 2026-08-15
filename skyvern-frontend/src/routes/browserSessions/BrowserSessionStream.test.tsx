import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserSessionStream } from "./BrowserSessionStream";
import {
  diagnosticForStatus,
  isTerminalStreamStatus,
  shouldReconnectStream,
} from "./BrowserSessionStream.utils";

vi.mock("@/hooks/useCredentialGetter", () => {
  // Stable identity: the stream effect keys on it, and a fresh function per
  // render would tear the socket down and clear the frame on every state change.
  const credentialGetter = async () => null;
  return { useCredentialGetter: () => credentialGetter };
});

vi.mock("@/routes/streaming/InteractiveStreamView", () => ({
  InteractiveStreamView: ({ streamImgSrc }: { streamImgSrc: string }) => (
    <div data-frame={streamImgSrc} data-testid="stream-frame" />
  ),
}));

class FakeStreamSocket {
  static instances: FakeStreamSocket[] = [];

  close = vi.fn();

  private listeners: Record<string, Array<(event: unknown) => void>> = {};

  constructor() {
    FakeStreamSocket.instances.push(this);
  }

  addEventListener(type: string, listener: (event: unknown) => void) {
    this.listeners[type] = [...(this.listeners[type] ?? []), listener];
  }

  removeEventListener() {}

  emit(type: string, event: unknown) {
    for (const listener of this.listeners[type] ?? []) {
      listener(event);
    }
  }

  emitStreamMessage(message: Record<string, unknown>) {
    this.emit("message", { data: JSON.stringify(message) });
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
  });

  it("replaces the last frame with the completed panel (SKY-13727)", async () => {
    render(<BrowserSessionStream browserSessionId="pbs_test" />);

    await waitFor(() => expect(FakeStreamSocket.instances).toHaveLength(1));
    const socket = FakeStreamSocket.instances[0]!;

    act(() => {
      socket.emitStreamMessage({ status: "running", screenshot: "abc123" });
    });
    expect(screen.getByTestId("stream-frame")).toBeTruthy();

    act(() => {
      socket.emitStreamMessage({ status: "completed" });
    });

    expect(screen.queryByTestId("stream-frame")).toBeNull();
    expect(screen.getByText("Browser session complete")).toBeTruthy();
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

    expect(screen.getByTestId("stream-frame").getAttribute("data-frame")).toBe(
      "third-frame",
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

    expect(screen.getByTestId("stream-frame").getAttribute("data-frame")).toBe(
      "current-frame",
    );
  });
});

describe("shouldReconnectStream", () => {
  it("reconnects non-terminal stream closes below the attempt limit", () => {
    expect(
      shouldReconnectStream({
        closeCode: 1006,
        closeReason: "",
        terminalStatusSeen: false,
        reconnectAttempts: 0,
      }),
    ).toBe(true);
  });

  it("does not reconnect terminal, VNC fallback, normal, or exhausted stream closes", () => {
    expect(
      shouldReconnectStream({
        closeCode: 1000,
        closeReason: "",
        terminalStatusSeen: true,
        reconnectAttempts: 0,
      }),
    ).toBe(false);
    expect(
      shouldReconnectStream({
        closeCode: 1000,
        closeReason: "",
        terminalStatusSeen: false,
        reconnectAttempts: 0,
      }),
    ).toBe(false);
    expect(
      shouldReconnectStream({
        closeCode: 4001,
        closeReason: "use-vnc-streaming",
        terminalStatusSeen: false,
        reconnectAttempts: 0,
      }),
    ).toBe(false);
    expect(
      shouldReconnectStream({
        closeCode: 1006,
        closeReason: "",
        terminalStatusSeen: false,
        reconnectAttempts: 20,
      }),
    ).toBe(false);
  });
});
