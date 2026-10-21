import { useCallback, useEffect, useRef, useState } from "react";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useLogging } from "@/hooks/useLogging";
import { getCredentialParam } from "@/util/env";
import { copyText } from "@/util/copyText";
import { useClientIdStore } from "@/store/useClientIdStore";
import {
  mouseButtonName,
  getModifiers,
  mapCoordinates,
  mapMouseCoordinates,
  virtualKeyCodeFor,
} from "./cdpInputUtils";

// 4411 is emitted mid-session (page resolution or dispatch failed), so unlike a setup-time close it
// leaves a channel the user still believes is live; without a reconnect, input is dead until reload.
const RECONNECTABLE_CODES = new Set([1006, 1011, 4408, 4410, 4411]);
const MOUSE_MOVE_BUFFER_THRESHOLD = 64 * 1024;

interface UseCdpInputOptions {
  inputWsUrl: string | null;
  interactive: boolean;
  viewportWidth: number;
  viewportHeight: number;
  onClipboardPaste?: (text: string) => void;
  onClipboardPasteError?: () => void;
  onClipboardCopy?: () => void;
  // Also deliver Cmd/Ctrl+C to the remote page, so apps with their own
  // selection model (canvas grids, editors) still copy there.
  forwardCopyShortcut?: boolean;
  onInput?: () => void;
}

interface UseCdpInputReturn {
  userIsControlling: boolean;
  setUserIsControlling: (v: boolean) => void;
  inputReady: boolean;
  containerRef: React.RefObject<HTMLDivElement>;
  handlers: {
    handleMouseDown: (e: React.MouseEvent<HTMLImageElement>) => void;
    handleMouseUp: (e: React.MouseEvent<HTMLImageElement>) => void;
    handleMouseMove: (e: React.MouseEvent<HTMLImageElement>) => void;
    handleKeyDown: (e: React.KeyboardEvent) => void;
    handleKeyUp: (e: React.KeyboardEvent) => void;
    handlePaste: (e: React.ClipboardEvent) => void;
  };
  navigate: (url: string) => void;
  historyNavigate: (action: HistoryAction) => void;
  navigateError: string | null;
  pasteClipboard: () => void;
}

export type HistoryAction = "back" | "forward" | "reload";

const HISTORY_EVENT_TYPES: Record<HistoryAction, string> = {
  back: "goBackEvent",
  forward: "goForwardEvent",
  reload: "reloadEvent",
};

const NAVIGATE_ERROR_MESSAGES: Record<string, string> = {
  blocked: "That destination isn't allowed.",
  invalid_url: "Enter a valid http(s) URL.",
};

function isShortcut(e: React.KeyboardEvent, key: string): boolean {
  return (e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === key;
}

function editingCommandsFor(e: React.KeyboardEvent): string[] {
  if (isShortcut(e, "a")) {
    return ["selectAll"];
  }
  if (e.altKey && !e.metaKey && !e.ctrlKey) {
    if (e.key === "Enter") {
      return ["insertNewline"];
    }
    if (e.key === "ArrowLeft") {
      return [e.shiftKey ? "moveWordLeftAndModifySelection" : "moveWordLeft"];
    }
    if (e.key === "ArrowRight") {
      return [e.shiftKey ? "moveWordRightAndModifySelection" : "moveWordRight"];
    }
  }
  if (e.metaKey && !e.ctrlKey && !e.altKey) {
    if (e.key === "ArrowLeft") {
      return [
        e.shiftKey
          ? "moveToLeftEndOfLineAndModifySelection"
          : "moveToLeftEndOfLine",
      ];
    }
    if (e.key === "ArrowRight") {
      return [
        e.shiftKey
          ? "moveToRightEndOfLineAndModifySelection"
          : "moveToRightEndOfLine",
      ];
    }
  }
  if (!e.metaKey && !e.ctrlKey && !e.altKey) {
    if (e.key === "Backspace") return ["deleteBackward"];
    if (e.key === "Delete") return ["deleteForward"];
    if (e.key === "Enter") return ["insertNewline"];
  }
  return [];
}

function mouseButtonNameForButtons(buttons: number): string {
  if (buttons & 1) return "left";
  if (buttons & 2) return "right";
  if (buttons & 4) return "middle";
  return "none";
}

function isEditableTarget(target: EventTarget | null) {
  return (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    (target instanceof HTMLElement && target.isContentEditable)
  );
}

export function useCdpInput({
  inputWsUrl,
  interactive,
  viewportWidth,
  viewportHeight,
  onClipboardPaste,
  onClipboardPasteError,
  onClipboardCopy,
  forwardCopyShortcut = false,
  onInput,
}: UseCdpInputOptions): UseCdpInputReturn {
  const [userIsControlling, setUserIsControlling] = useState(false);
  const [inputReady, setInputReady] = useState(false);
  const [navigateError, setNavigateError] = useState<string | null>(null);
  const credentialGetter = useCredentialGetter();
  const logging = useLogging();
  const clientId = useClientIdStore((s) => s.clientId);

  const inputSocketRef = useRef<WebSocket | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const userIsControllingRef = useRef(false);
  const inputReconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const inputReconnectAttemptsRef = useRef(0);
  const inputStoppedRef = useRef(false);
  const parseFailureLoggedRef = useRef(false);
  const gaveUpLoggedRef = useRef(false);
  const inputEventCountRef = useRef(0);
  const wheelAccumulatorRef = useRef<{
    deltaX: number;
    deltaY: number;
    x: number;
    y: number;
    modifiers: number;
  } | null>(null);
  const wheelAnimationFrameRef = useRef<number | null>(null);
  const pendingMouseMoveRef = useRef<Record<string, unknown> | null>(null);
  const mouseMoveAnimationFrameRef = useRef<number | null>(null);
  const cancelPendingMouseMove = () => {
    if (mouseMoveAnimationFrameRef.current !== null) {
      cancelAnimationFrame(mouseMoveAnimationFrameRef.current);
      mouseMoveAnimationFrameRef.current = null;
    }
    pendingMouseMoveRef.current = null;
  };
  const interceptedClipboardKeysRef = useRef(new Set<string>());
  const onClipboardPasteRef = useRef(onClipboardPaste);
  const onClipboardPasteErrorRef = useRef(onClipboardPasteError);
  const onClipboardCopyRef = useRef(onClipboardCopy);
  const onInputRef = useRef(onInput);
  onClipboardPasteRef.current = onClipboardPaste;
  onClipboardPasteErrorRef.current = onClipboardPasteError;
  onClipboardCopyRef.current = onClipboardCopy;
  const forwardCopyShortcutRef = useRef(forwardCopyShortcut);
  forwardCopyShortcutRef.current = forwardCopyShortcut;
  onInputRef.current = onInput;

  const pasteClipboard = useCallback(() => {
    if (!interactive || !userIsControlling || !onClipboardPasteRef.current) {
      return;
    }
    if (typeof navigator.clipboard?.readText !== "function") {
      onClipboardPasteErrorRef.current?.();
      return;
    }
    navigator.clipboard
      .readText()
      .then((text) => onClipboardPasteRef.current?.(text))
      .catch((error) => {
        console.error("Failed to read clipboard contents:", error);
        onClipboardPasteErrorRef.current?.();
      });
  }, [interactive, userIsControlling]);

  useEffect(() => {
    if (!interactive || !inputWsUrl) return;

    inputStoppedRef.current = false;
    inputReconnectAttemptsRef.current = 0;
    parseFailureLoggedRef.current = false;
    gaveUpLoggedRef.current = false;
    const streamTarget = inputWsUrl.match(
      /\/cdp_input\/(browser_session|workflow_run)\/([^?]+)/,
    );
    const targetType = streamTarget?.[1];
    const targetId = streamTarget?.[2]
      ? decodeURIComponent(streamTarget[2])
      : null;
    const browserSessionId = targetType === "browser_session" ? targetId : null;
    const workflowRunId = targetType === "workflow_run" ? targetId : null;

    function connectInputWs(credentialParam: string) {
      if (inputStoppedRef.current) return;
      if (inputSocketRef.current) {
        inputSocketRef.current.close();
      }
      const ws = new WebSocket(
        `${inputWsUrl}?client_id=${clientId}&${credentialParam}`,
      );
      inputSocketRef.current = ws;

      ws.addEventListener("open", () => {
        if (inputSocketRef.current !== ws) return;
        console.log("[cdp-input] WebSocket connected");
        if (userIsControllingRef.current) {
          ws.send(JSON.stringify({ kind: "take-control" }));
        }
      });
      ws.addEventListener("error", (e) => {
        console.error("[cdp-input] WebSocket error", e);
      });
      ws.addEventListener("message", (event) => {
        if (inputSocketRef.current !== ws) return;
        try {
          const msg = JSON.parse(event.data);
          if (msg.kind === "ready") {
            console.log(
              "[cdp-input] Server ready, sending current control state",
            );
            inputReconnectAttemptsRef.current = 0;
            setInputReady(true);
            setNavigateError(null);
            if (userIsControllingRef.current) {
              ws.send(JSON.stringify({ kind: "take-control" }));
            }
          }
          if (msg.kind === "navigate-error") {
            setNavigateError(
              NAVIGATE_ERROR_MESSAGES[msg.reason] ??
                "Couldn't navigate to that URL.",
            );
          }
          if (
            msg.kind === "copied-text" &&
            typeof msg.text === "string" &&
            msg.text
          ) {
            void copyText(msg.text);
          }
        } catch {
          if (!parseFailureLoggedRef.current) {
            parseFailureLoggedRef.current = true;
            logging.warn("Stream message parse failed", {
              stream: "cdp_input",
              browser_session_id: browserSessionId,
              workflow_run_id: workflowRunId,
            });
          }
        }
      });
      ws.addEventListener("close", (event) => {
        console.log("[cdp-input] WebSocket closed", event.code, event.reason);
        if (inputSocketRef.current !== ws) return;
        setInputReady(false);
        userIsControllingRef.current = false;
        setUserIsControlling(false);
        inputSocketRef.current = null;

        if (!inputStoppedRef.current && RECONNECTABLE_CODES.has(event.code)) {
          if (inputReconnectTimerRef.current) {
            clearTimeout(inputReconnectTimerRef.current);
          }
          inputReconnectTimerRef.current = setTimeout(() => {
            reconnectInputWs();
          }, 2000);
        }
      });
    }

    async function reconnectInputWs() {
      if (inputStoppedRef.current) return;
      if (inputReconnectAttemptsRef.current >= 5) {
        console.log("[cdp-input] Max reconnect attempts reached, giving up");
        if (!gaveUpLoggedRef.current) {
          gaveUpLoggedRef.current = true;
          logging.warn("Stream gave up", {
            stream: "cdp_input",
            browser_session_id: browserSessionId,
            workflow_run_id: workflowRunId,
            reason: "reconnect_exhausted",
            reconnect_attempts: inputReconnectAttemptsRef.current,
          });
        }
        return;
      }
      inputReconnectAttemptsRef.current += 1;
      console.log(
        `[cdp-input] Reconnecting (attempt ${inputReconnectAttemptsRef.current}/5)`,
      );
      try {
        const credentialParam = await getCredentialParam(credentialGetter);
        connectInputWs(credentialParam);
      } catch (e) {
        console.error("[cdp-input] Failed to get credentials for reconnect", e);
      }
    }

    getCredentialParam(credentialGetter).then((credentialParam) => {
      connectInputWs(credentialParam);
    });

    return () => {
      inputStoppedRef.current = true;
      if (inputReconnectTimerRef.current) {
        clearTimeout(inputReconnectTimerRef.current);
        inputReconnectTimerRef.current = null;
      }
      if (inputSocketRef.current) {
        inputSocketRef.current.close();
        inputSocketRef.current = null;
      }
      cancelPendingMouseMove();
    };
  }, [interactive, inputWsUrl, credentialGetter, clientId, logging]);

  useEffect(() => {
    userIsControllingRef.current = userIsControlling;
    return () => {
      cancelPendingMouseMove();
    };
  }, [userIsControlling]);

  useEffect(() => {
    // A stale error from a prior take-control stint should not linger next to a page
    // that has since been ceded, retaken, and possibly navigated by other means.
    setNavigateError(null);
    const ws = inputSocketRef.current;
    const kind = userIsControlling ? "take-control" : "cede-control";
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return;
    }
    console.log(`[cdp-input] Sending ${kind}`);
    ws.send(JSON.stringify({ kind }));
    if (userIsControlling) {
      inputEventCountRef.current = 0;
    }
  }, [userIsControlling]);

  useEffect(() => {
    if (userIsControlling) {
      containerRef.current?.focus();
    } else {
      containerRef.current?.blur();
    }
  }, [userIsControlling]);

  // Wheel event listener (needs non-passive to preventDefault)
  useEffect(() => {
    if (!interactive || !userIsControlling) return;
    const el = containerRef.current;
    if (!el) return;

    const flushWheelEvents = () => {
      wheelAnimationFrameRef.current = null;
      const event = wheelAccumulatorRef.current;
      wheelAccumulatorRef.current = null;
      const ws = inputSocketRef.current;
      if (
        event &&
        (event.deltaX !== 0 || event.deltaY !== 0) &&
        ws?.readyState === WebSocket.OPEN
      ) {
        ws.send(
          JSON.stringify({
            type: "wheelEvent",
            x: event.x,
            y: event.y,
            deltaX: Math.round(event.deltaX),
            deltaY: Math.round(event.deltaY),
            modifiers: event.modifiers,
          }),
        );
      }
    };

    const handler = (e: WheelEvent) => {
      e.preventDefault();
      const ws = inputSocketRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;

      const img = el.querySelector("img");
      if (!img) return;

      const rect = img.getBoundingClientRect();
      const coords = mapCoordinates(
        e.clientX,
        e.clientY,
        rect,
        viewportWidth,
        viewportHeight,
      );
      if (!coords) return;

      const accumulated = wheelAccumulatorRef.current;
      wheelAccumulatorRef.current = {
        deltaX: (accumulated?.deltaX ?? 0) + e.deltaX,
        deltaY: (accumulated?.deltaY ?? 0) + e.deltaY,
        x: coords.x,
        y: coords.y,
        modifiers: getModifiers(e),
      };
      if (accumulated === null) {
        wheelAnimationFrameRef.current =
          requestAnimationFrame(flushWheelEvents);
      }
    };

    el.addEventListener("wheel", handler, { passive: false });
    return () => {
      el.removeEventListener("wheel", handler);
      if (wheelAnimationFrameRef.current !== null) {
        cancelAnimationFrame(wheelAnimationFrameRef.current);
        wheelAnimationFrameRef.current = null;
      }
      wheelAccumulatorRef.current = null;
    };
  }, [interactive, userIsControlling, viewportWidth, viewportHeight]);

  const sendInputEvent = useCallback((payload: Record<string, unknown>) => {
    const ws = inputSocketRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      if (inputEventCountRef.current < 3) {
        console.log(
          "[cdp-input] Event dropped (ws not open):",
          payload.type,
          payload.eventType,
        );
        inputEventCountRef.current++;
      }
      return;
    }
    if (inputEventCountRef.current < 3) {
      console.log("[cdp-input] Sending:", payload.type, payload.eventType);
      inputEventCountRef.current++;
    }
    onInputRef.current?.();
    ws.send(JSON.stringify(payload));
  }, []);

  const flushMouseMove = useCallback(() => {
    mouseMoveAnimationFrameRef.current = null;
    const payload = pendingMouseMoveRef.current;
    pendingMouseMoveRef.current = null;
    if (payload) {
      sendInputEvent(payload);
    }
  }, [sendInputEvent]);

  const handleMouseDown = useCallback(
    (e: React.MouseEvent<HTMLImageElement>) => {
      cancelPendingMouseMove();
      if (!interactive || !userIsControlling) return;
      const coords = mapMouseCoordinates(e, viewportWidth, viewportHeight);
      if (!coords) return;
      sendInputEvent({
        type: "mouseEvent",
        eventType: "mousePressed",
        x: coords.x,
        y: coords.y,
        button: mouseButtonName(e.button),
        buttons: e.buttons,
        clickCount: Math.max(1, Math.min(e.detail || 1, 3)),
        modifiers: getModifiers(e),
      });
    },
    [
      interactive,
      userIsControlling,
      viewportWidth,
      viewportHeight,
      sendInputEvent,
    ],
  );

  const handleMouseUp = useCallback(
    (e: React.MouseEvent<HTMLImageElement>) => {
      cancelPendingMouseMove();
      if (!interactive || !userIsControlling) return;
      const coords = mapMouseCoordinates(e, viewportWidth, viewportHeight);
      if (!coords) return;
      sendInputEvent({
        type: "mouseEvent",
        eventType: "mouseReleased",
        x: coords.x,
        y: coords.y,
        button: mouseButtonName(e.button),
        buttons: e.buttons,
        clickCount: Math.max(1, Math.min(e.detail || 1, 3)),
        modifiers: getModifiers(e),
      });
    },
    [
      interactive,
      userIsControlling,
      viewportWidth,
      viewportHeight,
      sendInputEvent,
    ],
  );

  const handleMouseMove = useCallback(
    (e: React.MouseEvent<HTMLImageElement>) => {
      if (!interactive || !userIsControlling) return;
      const coords = mapMouseCoordinates(e, viewportWidth, viewportHeight);
      if (!coords) return;
      const payload = {
        type: "mouseEvent",
        eventType: "mouseMoved",
        x: coords.x,
        y: coords.y,
        button: mouseButtonNameForButtons(e.buttons),
        buttons: e.buttons,
        clickCount: 0,
        modifiers: getModifiers(e),
      };
      const ws = inputSocketRef.current;
      if (
        ws?.readyState === WebSocket.OPEN &&
        ws.bufferedAmount > MOUSE_MOVE_BUFFER_THRESHOLD
      ) {
        pendingMouseMoveRef.current = payload;
        if (mouseMoveAnimationFrameRef.current === null) {
          mouseMoveAnimationFrameRef.current =
            requestAnimationFrame(flushMouseMove);
        }
        return;
      }
      cancelPendingMouseMove();
      sendInputEvent(payload);
    },
    [
      interactive,
      userIsControlling,
      viewportWidth,
      viewportHeight,
      flushMouseMove,
      sendInputEvent,
    ],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!interactive || !userIsControlling) return;

      if (isShortcut(e, "v")) {
        interceptedClipboardKeysRef.current.add(e.code);
        e.stopPropagation();
        if (!onClipboardPasteRef.current) {
          // Keep the native paste event alive for the direct CDP fallback.
          return;
        }
        e.preventDefault();
        pasteClipboard();
        return;
      }

      if (isShortcut(e, "c")) {
        e.preventDefault();
        e.stopPropagation();
        if (onClipboardCopyRef.current) {
          onClipboardCopyRef.current();
          if (!forwardCopyShortcutRef.current) {
            interceptedClipboardKeysRef.current.add(e.code);
            return;
          }
        } else {
          interceptedClipboardKeysRef.current.add(e.code);
          sendInputEvent({ type: "copySelectedText" });
          return;
        }
      }

      e.preventDefault();
      const isPrintable =
        e.key.length === 1 && !e.metaKey && (!e.ctrlKey || e.altKey);
      const windowsVirtualKeyCode = virtualKeyCodeFor(e);
      const commands = editingCommandsFor(e);
      const payload: Record<string, unknown> = {
        type: "keyEvent",
        eventType: isPrintable ? "keyDown" : "rawKeyDown",
        key: e.key,
        code: e.code,
        text: isPrintable ? e.key : "",
        modifiers: getModifiers(e),
      };
      if (windowsVirtualKeyCode !== undefined) {
        payload.windowsVirtualKeyCode = windowsVirtualKeyCode;
      }
      if (commands.length) {
        payload.commands = commands;
      }
      sendInputEvent(payload);
    },
    [interactive, userIsControlling, sendInputEvent, pasteClipboard],
  );

  const handlePaste = useCallback(
    (e: React.ClipboardEvent) => {
      if (!interactive || !userIsControlling || isEditableTarget(e.target))
        return;
      const text = e.clipboardData.getData("text/plain");
      if (!text) return;
      e.preventDefault();
      e.stopPropagation();
      if (onClipboardPasteRef.current) {
        onClipboardPasteRef.current(text);
      } else {
        sendInputEvent({ type: "insertText", text });
      }
    },
    [interactive, userIsControlling, sendInputEvent],
  );

  const handleKeyUp = useCallback(
    (e: React.KeyboardEvent) => {
      if (!interactive || !userIsControlling) return;
      e.preventDefault();
      if (interceptedClipboardKeysRef.current.delete(e.code)) {
        return;
      }
      const windowsVirtualKeyCode = virtualKeyCodeFor(e);
      const payload: Record<string, unknown> = {
        type: "keyEvent",
        eventType: "keyUp",
        key: e.key,
        code: e.code,
        modifiers: getModifiers(e),
      };
      if (windowsVirtualKeyCode !== undefined) {
        payload.windowsVirtualKeyCode = windowsVirtualKeyCode;
      }
      sendInputEvent(payload);
    },
    [interactive, userIsControlling, sendInputEvent],
  );

  const navigate = useCallback(
    (url: string) => {
      if (!interactive || !userIsControlling) return;
      setNavigateError(null);
      sendInputEvent({ type: "navigateEvent", url });
    },
    [interactive, userIsControlling, sendInputEvent],
  );

  const historyNavigate = useCallback(
    (action: HistoryAction) => {
      if (!interactive || !userIsControlling) return;
      setNavigateError(null);
      sendInputEvent({ type: HISTORY_EVENT_TYPES[action] });
    },
    [interactive, userIsControlling, sendInputEvent],
  );

  return {
    userIsControlling,
    setUserIsControlling,
    inputReady,
    containerRef,
    handlers: {
      handleMouseDown,
      handleMouseUp,
      handleMouseMove,
      handleKeyDown,
      handleKeyUp,
      handlePaste,
    },
    navigate,
    historyNavigate,
    navigateError,
    pasteClipboard,
  };
}
