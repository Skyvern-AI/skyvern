import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useCdpInput } from "./useCdpInput";

const mocks = vi.hoisted(() => ({
  credentialGetter: vi.fn(async () => null),
  getCredentialParam: vi.fn(async () => "token=Bearer+test-token"),
  copyText: vi.fn(async () => true),
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

vi.mock("@/util/copyText", () => ({
  copyText: mocks.copyText,
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

  emitMessage(data: string) {
    const event = new MessageEvent("message", { data });
    for (const listener of this.listeners.message ?? []) {
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

async function renderControllingInputHook() {
  const { result } = renderHook(() =>
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
  act(() => {
    result.current.setUserIsControlling(true);
  });

  return result;
}

function fakeKeyboardEvent(
  key: string,
  code: string,
  overrides: Partial<React.KeyboardEvent> = {},
): React.KeyboardEvent {
  return {
    key,
    code,
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
    ...overrides,
  } as unknown as React.KeyboardEvent;
}

function fakeMouseEvent({
  button = 0,
  buttons = 0,
  detail = 1,
}: {
  button?: number;
  buttons?: number;
  detail?: number;
}): React.MouseEvent<HTMLImageElement> {
  return {
    button,
    buttons,
    detail,
    clientX: 640,
    clientY: 360,
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    currentTarget: {
      getBoundingClientRect: () => ({
        left: 0,
        top: 0,
        width: 1280,
        height: 720,
      }),
    },
  } as unknown as React.MouseEvent<HTMLImageElement>;
}

function latestSocketSend() {
  const socket = MockWebSocket.instances[MockWebSocket.instances.length - 1];
  if (!socket) throw new Error("No WebSocket was constructed");
  return socket.send;
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

describe("useCdpInput key handling", () => {
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

  // SKY-13682: Delete (and every other non-printable key - Backspace, Enter, arrows, ...) was a
  // no-op in the remote page. CDP's Input.dispatchKeyEvent only performs the default edit action
  // when non-printable keys carry a windowsVirtualKeyCode, dispatched as eventType "rawKeyDown".
  it("sends windowsVirtualKeyCode and eventType rawKeyDown for Delete", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("Delete", "Delete"),
      );
    });

    const send = latestSocketSend();
    expect(send).toHaveBeenCalledWith(
      JSON.stringify({
        type: "keyEvent",
        eventType: "rawKeyDown",
        key: "Delete",
        code: "Delete",
        text: "",
        modifiers: 0,
        windowsVirtualKeyCode: 46,
        commands: ["deleteForward"],
      }),
    );
  });

  it("sends windowsVirtualKeyCode and eventType rawKeyDown for Backspace", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("Backspace", "Backspace"),
      );
    });

    const send = latestSocketSend();
    expect(send).toHaveBeenCalledWith(
      JSON.stringify({
        type: "keyEvent",
        eventType: "rawKeyDown",
        key: "Backspace",
        code: "Backspace",
        text: "",
        modifiers: 0,
        windowsVirtualKeyCode: 8,
        commands: ["deleteBackward"],
      }),
    );
  });

  // Numpad Delete with NumLock off reports key "Delete" (same as the main Delete key) with a
  // different `code`; the vk lookup keys off `key` first so both resolve to the same edit action.
  it("resolves numpad Delete (NumLock off) the same as the main Delete key", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("Delete", "NumpadDecimal"),
      );
    });

    const send = latestSocketSend();
    expect(send).toHaveBeenCalledWith(
      JSON.stringify({
        type: "keyEvent",
        eventType: "rawKeyDown",
        key: "Delete",
        code: "NumpadDecimal",
        text: "",
        modifiers: 0,
        windowsVirtualKeyCode: 46,
        commands: ["deleteForward"],
      }),
    );
  });

  it("still sends printable text entry as keyDown with no windowsVirtualKeyCode", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyDown(fakeKeyboardEvent("a", "KeyA"));
    });

    const send = latestSocketSend();
    expect(send).toHaveBeenCalledWith(
      JSON.stringify({
        type: "keyEvent",
        eventType: "keyDown",
        key: "a",
        code: "KeyA",
        text: "a",
        modifiers: 0,
      }),
    );
  });

  it("includes windowsVirtualKeyCode on keyUp for non-printable keys", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyUp(
        fakeKeyboardEvent("Delete", "Delete"),
      );
    });

    const send = latestSocketSend();
    expect(send).toHaveBeenCalledWith(
      JSON.stringify({
        type: "keyEvent",
        eventType: "keyUp",
        key: "Delete",
        code: "Delete",
        modifiers: 0,
        windowsVirtualKeyCode: 46,
      }),
    );
  });

  it("sends selectAll instead of printable text for Command+A", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("a", "KeyA", { metaKey: true }),
      );
    });

    expect(latestSocketSend()).toHaveBeenCalledWith(
      JSON.stringify({
        type: "keyEvent",
        eventType: "rawKeyDown",
        key: "a",
        code: "KeyA",
        text: "",
        modifiers: 4,
        commands: ["selectAll"],
      }),
    );
  });

  it("maps Command+ArrowLeft to the macOS line-boundary command", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("ArrowLeft", "ArrowLeft", { metaKey: true }),
      );
    });

    expect(latestSocketSend()).toHaveBeenCalledWith(
      JSON.stringify({
        type: "keyEvent",
        eventType: "rawKeyDown",
        key: "ArrowLeft",
        code: "ArrowLeft",
        text: "",
        modifiers: 4,
        windowsVirtualKeyCode: 37,
        commands: ["moveToLeftEndOfLine"],
      }),
    );
  });

  it("maps Command+Shift+ArrowRight to line selection", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("ArrowRight", "ArrowRight", {
          metaKey: true,
          shiftKey: true,
        }),
      );
    });

    expect(latestSocketSend()).toHaveBeenCalledWith(
      JSON.stringify({
        type: "keyEvent",
        eventType: "rawKeyDown",
        key: "ArrowRight",
        code: "ArrowRight",
        text: "",
        modifiers: 12,
        windowsVirtualKeyCode: 39,
        commands: ["moveToRightEndOfLineAndModifySelection"],
      }),
    );
  });

  it("maps Option+ArrowLeft to word-boundary movement", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("ArrowLeft", "ArrowLeft", { altKey: true }),
      );
    });

    expect(latestSocketSend()).toHaveBeenCalledWith(
      JSON.stringify({
        type: "keyEvent",
        eventType: "rawKeyDown",
        key: "ArrowLeft",
        code: "ArrowLeft",
        text: "",
        modifiers: 1,
        windowsVirtualKeyCode: 37,
        commands: ["moveWordLeft"],
      }),
    );
  });

  it("maps Option+Shift+ArrowRight to word selection", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("ArrowRight", "ArrowRight", {
          altKey: true,
          shiftKey: true,
        }),
      );
    });

    expect(latestSocketSend()).toHaveBeenCalledWith(
      JSON.stringify({
        type: "keyEvent",
        eventType: "rawKeyDown",
        key: "ArrowRight",
        code: "ArrowRight",
        text: "",
        modifiers: 9,
        windowsVirtualKeyCode: 39,
        commands: ["moveWordRightAndModifySelection"],
      }),
    );
  });

  it("sends insertNewline for Shift+Enter", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("Enter", "Enter", { shiftKey: true }),
      );
    });

    expect(latestSocketSend()).toHaveBeenCalledWith(
      JSON.stringify({
        type: "keyEvent",
        eventType: "rawKeyDown",
        key: "Enter",
        code: "Enter",
        text: "",
        modifiers: 8,
        windowsVirtualKeyCode: 13,
        commands: ["insertNewline"],
      }),
    );
  });

  it("copies the remote selection to the local clipboard for Command+C", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("c", "KeyC", { metaKey: true }),
      );
    });

    expect(latestSocketSend()).toHaveBeenLastCalledWith(
      JSON.stringify({ type: "copySelectedText" }),
    );

    act(() => {
      MockWebSocket.instances
        .at(-1)
        ?.emitMessage(
          JSON.stringify({ kind: "copied-text", text: "selected remotely" }),
        );
    });
    expect(mocks.copyText).toHaveBeenCalledWith("selected remotely");
  });

  it("inserts text from a local paste event without sending Command+V", async () => {
    const result = await renderControllingInputHook();
    const keyDown = fakeKeyboardEvent("v", "KeyV", { metaKey: true });

    act(() => {
      result.current.handlers.handleKeyDown(keyDown);
    });

    const pasteEvent = {
      clipboardData: {
        getData: vi.fn(() => "pasted locally"),
      },
      preventDefault: vi.fn(),
      stopPropagation: vi.fn(),
    } as unknown as React.ClipboardEvent;
    act(() => {
      result.current.handlers.handlePaste(pasteEvent);
    });

    expect(latestSocketSend()).toHaveBeenLastCalledWith(
      JSON.stringify({ type: "insertText", text: "pasted locally" }),
    );
    expect(pasteEvent.preventDefault).toHaveBeenCalled();
  });

  it("preserves the pressed button mask while moving the mouse", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleMouseMove(fakeMouseEvent({ buttons: 1 }));
    });

    expect(latestSocketSend()).toHaveBeenCalledWith(
      JSON.stringify({
        type: "mouseEvent",
        eventType: "mouseMoved",
        x: 640,
        y: 360,
        button: "left",
        buttons: 1,
        clickCount: 0,
        modifiers: 0,
      }),
    );
  });

  it("forwards the browser click count for double-click selection", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleMouseDown(
        fakeMouseEvent({ button: 0, buttons: 1, detail: 2 }),
      );
    });

    expect(latestSocketSend()).toHaveBeenCalledWith(
      JSON.stringify({
        type: "mouseEvent",
        eventType: "mousePressed",
        x: 640,
        y: 360,
        button: "left",
        buttons: 1,
        clickCount: 2,
        modifiers: 0,
      }),
    );
  });
});

describe("useCdpInput navigate", () => {
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

  it("does not send a navigateEvent when the user has not taken control", async () => {
    const { result } = renderHook(() =>
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
    const socket = MockWebSocket.instances[0]!;
    socket.send.mockClear();

    act(() => {
      result.current.navigate("https://example.com");
    });

    expect(socket.send).not.toHaveBeenCalled();
  });

  it("sends a navigateEvent over the existing input socket once controlling", async () => {
    const { result } = renderHook(() =>
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
    act(() => {
      result.current.setUserIsControlling(true);
    });
    const socket = MockWebSocket.instances[0]!;
    socket.send.mockClear();

    act(() => {
      result.current.navigate("https://example.com");
    });

    expect(socket.send).toHaveBeenCalledWith(
      JSON.stringify({ type: "navigateEvent", url: "https://example.com" }),
    );
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it("surfaces a navigate-error message the server sends back", async () => {
    const { result } = renderHook(() =>
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
    expect(result.current.navigateError).toBeNull();

    const socket = MockWebSocket.instances[0]!;
    act(() => {
      socket.emitMessage(
        JSON.stringify({ kind: "navigate-error", reason: "blocked" }),
      );
    });

    expect(result.current.navigateError).toBeTruthy();
  });

  it("clears a stale navigateError when control is ceded", async () => {
    const { result } = renderHook(() =>
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
    act(() => {
      result.current.setUserIsControlling(true);
    });
    const socket = MockWebSocket.instances[0]!;
    act(() => {
      socket.emitMessage(
        JSON.stringify({ kind: "navigate-error", reason: "blocked" }),
      );
    });
    expect(result.current.navigateError).toBeTruthy();

    act(() => {
      result.current.setUserIsControlling(false);
    });

    expect(result.current.navigateError).toBeNull();
  });

  it("clears a stale navigateError on reconnect (fresh ready message)", async () => {
    const { result } = renderHook(() =>
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
    const socket = MockWebSocket.instances[0]!;
    act(() => {
      socket.emitMessage(
        JSON.stringify({ kind: "navigate-error", reason: "blocked" }),
      );
    });
    expect(result.current.navigateError).toBeTruthy();

    act(() => {
      socket.emitMessage(JSON.stringify({ kind: "ready" }));
    });

    expect(result.current.navigateError).toBeNull();
  });
});
