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

async function renderControllingInputHook(
  clipboardCallbacks: {
    onClipboardPaste?: (text: string) => void;
    onClipboardCopy?: () => void;
  } = {},
) {
  const { result } = renderHook(() =>
    useCdpInput({
      inputWsUrl: "wss://input.test",
      interactive: true,
      viewportWidth: 1280,
      viewportHeight: 720,
      ...clipboardCallbacks,
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

async function renderWheelInputHook() {
  const hook = renderHook(() =>
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

  const container = document.createElement("div");
  const image = document.createElement("img");
  vi.spyOn(image, "getBoundingClientRect").mockReturnValue({
    x: 0,
    y: 0,
    top: 0,
    right: 1280,
    bottom: 720,
    left: 0,
    width: 1280,
    height: 720,
    toJSON: () => ({}),
  });
  container.appendChild(image);
  Object.defineProperty(hook.result.current.containerRef, "current", {
    configurable: true,
    value: container,
  });

  act(() => {
    hook.result.current.setUserIsControlling(true);
  });
  latestSocketSend().mockClear();

  return { ...hook, container };
}

function fakeKeyboardEvent(
  key: string,
  code: string,
  modifiers: Partial<
    Pick<React.KeyboardEvent, "altKey" | "ctrlKey" | "metaKey" | "shiftKey">
  > = {},
): React.KeyboardEvent {
  return {
    key,
    code,
    altKey: modifiers.altKey ?? false,
    ctrlKey: modifiers.ctrlKey ?? false,
    metaKey: modifiers.metaKey ?? false,
    shiftKey: modifiers.shiftKey ?? false,
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
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

  it("requires and sends a fresh take-control after reconnect", async () => {
    const result = await renderControllingInputHook();
    const firstSocket = MockWebSocket.instances[0]!;
    expect(firstSocket.send).toHaveBeenCalledWith(
      JSON.stringify({ kind: "take-control" }),
    );

    closeLatestSocket(4411);
    await advanceReconnectDelay();
    const reconnectedSocket = MockWebSocket.instances[1]!;
    act(() => {
      reconnectedSocket.emitMessage(JSON.stringify({ kind: "ready" }));
    });
    expect(result.current.userIsControlling).toBe(false);

    reconnectedSocket.send.mockClear();
    act(() => {
      result.current.setUserIsControlling(true);
    });
    expect(reconnectedSocket.send).toHaveBeenCalledWith(
      JSON.stringify({ kind: "take-control" }),
    );
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

  it("resets its five-attempt budget only after the fresh socket is ready", async () => {
    await renderInputHook();

    for (let attempt = 0; attempt < 4; attempt += 1) {
      closeLatestSocket(4410);
      await advanceReconnectDelay();
    }
    expect(MockWebSocket.instances).toHaveLength(5);

    act(() => {
      MockWebSocket.instances[MockWebSocket.instances.length - 1]!.emitMessage(
        JSON.stringify({ kind: "ready" }),
      );
    });
    for (let attempt = 0; attempt < 5; attempt += 1) {
      closeLatestSocket(4410);
      await advanceReconnectDelay();
    }
    expect(MockWebSocket.instances).toHaveLength(10);

    closeLatestSocket(4410);
    await advanceReconnectDelay();
    expect(MockWebSocket.instances).toHaveLength(10);
  });
});

describe("useCdpInput wheel handling", () => {
  let animationFrameCallback: FrameRequestCallback | undefined;
  let nextAnimationFrameId: number;
  let requestAnimationFrame: ReturnType<typeof vi.fn>;
  let cancelAnimationFrame: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    nextAnimationFrameId = 1;
    requestAnimationFrame = vi.fn((callback: FrameRequestCallback) => {
      animationFrameCallback = callback;
      return nextAnimationFrameId++;
    });
    vi.stubGlobal("requestAnimationFrame", requestAnimationFrame);
    cancelAnimationFrame = vi.fn();
    vi.stubGlobal("cancelAnimationFrame", cancelAnimationFrame);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("coalesces wheel events within one animation frame", async () => {
    const { container } = await renderWheelInputHook();
    expect(requestAnimationFrame).not.toHaveBeenCalled();
    const first = new WheelEvent("wheel", {
      clientX: 100,
      clientY: 200,
      deltaX: 1.2,
      deltaY: 2.4,
      ctrlKey: true,
      cancelable: true,
    });
    const second = new WheelEvent("wheel", {
      clientX: 300,
      clientY: 400,
      deltaX: 2.4,
      deltaY: 3.2,
      shiftKey: true,
      cancelable: true,
    });

    container.dispatchEvent(first);
    container.dispatchEvent(second);

    expect(first.defaultPrevented).toBe(true);
    expect(second.defaultPrevented).toBe(true);
    expect(requestAnimationFrame).toHaveBeenCalledOnce();
    expect(latestSocketSend()).not.toHaveBeenCalled();

    act(() => {
      animationFrameCallback?.(16);
    });

    expect(latestSocketSend()).toHaveBeenCalledOnce();
    expect(latestSocketSend()).toHaveBeenCalledWith(
      JSON.stringify({
        type: "wheelEvent",
        x: 300,
        y: 400,
        deltaX: 4,
        deltaY: 6,
        modifiers: 8,
      }),
    );
    expect(requestAnimationFrame).toHaveBeenCalledOnce();
  });

  it("cancels a pending wheel animation frame on cleanup", async () => {
    const { container, unmount } = await renderWheelInputHook();
    container.dispatchEvent(
      new WheelEvent("wheel", {
        clientX: 100,
        clientY: 200,
        deltaY: 1,
        cancelable: true,
      }),
    );

    unmount();

    expect(cancelAnimationFrame).toHaveBeenCalledWith(1);
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

  it("maps Option+Enter to newline insertion", async () => {
    const result = await renderControllingInputHook();

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("Enter", "Enter", { altKey: true }),
      );
    });

    expect(latestSocketSend()).toHaveBeenCalledWith(
      JSON.stringify({
        type: "keyEvent",
        eventType: "rawKeyDown",
        key: "Enter",
        code: "Enter",
        text: "",
        modifiers: 1,
        windowsVirtualKeyCode: 13,
        commands: ["insertNewline"],
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

  it("copies through direct CDP when no recording callback is present", async () => {
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

  it("copies through the recording callback when one is present", async () => {
    const onClipboardCopy = vi.fn();
    const result = await renderControllingInputHook({ onClipboardCopy });
    const send = latestSocketSend();
    send.mockClear();

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("c", "KeyC", { metaKey: true }),
      );
    });

    expect(onClipboardCopy).toHaveBeenCalledOnce();
    expect(send).not.toHaveBeenCalled();
  });

  it("inserts text from a local paste event when no recording callback is present", async () => {
    const result = await renderControllingInputHook();
    const send = latestSocketSend();
    send.mockClear();
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

    expect(keyDown.preventDefault).not.toHaveBeenCalled();
    expect(send).toHaveBeenLastCalledWith(
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

  it("sends Cmd+V through the recording message callback instead of CDP key events", async () => {
    const onClipboardPaste = vi.fn();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { readText: vi.fn(async () => "clipboard text") },
    });
    const result = await renderControllingInputHook({ onClipboardPaste });
    const send = latestSocketSend();
    send.mockClear();
    const keyDown = fakeKeyboardEvent("v", "KeyV", { metaKey: true });
    const keyUp = fakeKeyboardEvent("v", "KeyV");

    await act(async () => {
      result.current.handlers.handleKeyDown(keyDown);
      await Promise.resolve();
      result.current.handlers.handleKeyUp(keyUp);
    });

    expect(onClipboardPaste).toHaveBeenCalledWith("clipboard text");
    expect(send).not.toHaveBeenCalled();
    expect(keyDown.preventDefault).toHaveBeenCalled();
    expect(keyUp.preventDefault).toHaveBeenCalled();
  });

  it("drops a clipboard read that resolves after recording callbacks are removed", async () => {
    let resolveClipboard: ((text: string) => void) | undefined;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        readText: vi.fn(
          () =>
            new Promise<string>((resolve) => {
              resolveClipboard = resolve;
            }),
        ),
      },
    });
    const onClipboardPaste = vi.fn();
    const { result, rerender } = renderHook(
      ({ paste }: { paste?: (text: string) => void }) =>
        useCdpInput({
          inputWsUrl: "wss://input.test",
          interactive: true,
          viewportWidth: 1280,
          viewportHeight: 720,
          onClipboardPaste: paste,
        }),
      {
        initialProps: {
          paste: onClipboardPaste as ((text: string) => void) | undefined,
        },
      },
    );
    await act(async () => Promise.resolve());
    act(() => result.current.setUserIsControlling(true));

    act(() => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("v", "KeyV", { metaKey: true }),
      );
    });
    rerender({ paste: undefined });
    await act(async () => {
      resolveClipboard?.("late clipboard text");
      await Promise.resolve();
    });

    expect(onClipboardPaste).not.toHaveBeenCalled();
  });

  it("forwards an ordinary key pair after intercepting a clipboard chord", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { readText: vi.fn(async () => "clipboard text") },
    });
    const result = await renderControllingInputHook({
      onClipboardPaste: vi.fn(),
    });
    const send = latestSocketSend();
    send.mockClear();

    await act(async () => {
      result.current.handlers.handleKeyDown(
        fakeKeyboardEvent("v", "KeyV", { metaKey: true }),
      );
      await Promise.resolve();
      result.current.handlers.handleKeyUp(fakeKeyboardEvent("v", "KeyV"));
    });
    act(() => {
      result.current.handlers.handleKeyDown(fakeKeyboardEvent("v", "KeyV"));
      result.current.handlers.handleKeyUp(fakeKeyboardEvent("v", "KeyV"));
    });

    expect(send.mock.calls.map((call) => JSON.parse(String(call[0])))).toEqual([
      expect.objectContaining({ eventType: "keyDown", code: "KeyV" }),
      expect.objectContaining({ eventType: "keyUp", code: "KeyV" }),
    ]);
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
