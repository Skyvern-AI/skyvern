import { describe, expect, it } from "vitest";

import {
  isTerminalStreamStatus,
  shouldReconnectStream,
} from "./BrowserSessionStream.utils";

describe("isTerminalStreamStatus", () => {
  it("treats missing sessions as terminal stream statuses", () => {
    expect(isTerminalStreamStatus("not_found")).toBe(true);
    expect(isTerminalStreamStatus("running")).toBe(false);
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
