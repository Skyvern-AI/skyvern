import { useCallback, useEffect, useRef, useState } from "react";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getCredentialParam } from "@/util/env";
import { useClientIdStore } from "@/store/useClientIdStore";
import {
  mouseButtonName,
  getModifiers,
  mapCoordinates,
  mapMouseCoordinates,
} from "./cdpInputUtils";

const RECONNECTABLE_CODES = new Set([1006, 1011, 4408, 4410]);

interface UseCdpInputOptions {
  inputWsUrl: string | null;
  interactive: boolean;
  viewportWidth: number;
  viewportHeight: number;
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
  };
}

export function useCdpInput({
  inputWsUrl,
  interactive,
  viewportWidth,
  viewportHeight,
}: UseCdpInputOptions): UseCdpInputReturn {
  const [userIsControlling, setUserIsControlling] = useState(false);
  const [inputReady, setInputReady] = useState(false);
  const credentialGetter = useCredentialGetter();
  const clientId = useClientIdStore((s) => s.clientId);

  const inputSocketRef = useRef<WebSocket | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const lastMouseMoveRef = useRef<number>(0);
  const userIsControllingRef = useRef(false);
  const inputReconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const inputReconnectAttemptsRef = useRef(0);
  const inputStoppedRef = useRef(false);
  const inputEventCountRef = useRef(0);

  useEffect(() => {
    if (!interactive || !inputWsUrl) return;

    inputStoppedRef.current = false;
    inputReconnectAttemptsRef.current = 0;

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
            if (userIsControllingRef.current) {
              ws.send(JSON.stringify({ kind: "take-control" }));
            }
          }
        } catch {
          // ignore non-JSON messages
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
    };
  }, [interactive, inputWsUrl, credentialGetter, clientId]);

  useEffect(() => {
    userIsControllingRef.current = userIsControlling;
  }, [userIsControlling]);

  useEffect(() => {
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

      ws.send(
        JSON.stringify({
          type: "wheelEvent",
          x: coords.x,
          y: coords.y,
          deltaX: Math.round(e.deltaX),
          deltaY: Math.round(e.deltaY),
          modifiers: getModifiers(e),
        }),
      );
    };

    el.addEventListener("wheel", handler, { passive: false });
    return () => el.removeEventListener("wheel", handler);
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
    ws.send(JSON.stringify(payload));
  }, []);

  const handleMouseDown = useCallback(
    (e: React.MouseEvent<HTMLImageElement>) => {
      if (!interactive || !userIsControlling) return;
      const coords = mapMouseCoordinates(e, viewportWidth, viewportHeight);
      if (!coords) return;
      sendInputEvent({
        type: "mouseEvent",
        eventType: "mousePressed",
        x: coords.x,
        y: coords.y,
        button: mouseButtonName(e.button),
        clickCount: 1,
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
      if (!interactive || !userIsControlling) return;
      const coords = mapMouseCoordinates(e, viewportWidth, viewportHeight);
      if (!coords) return;
      sendInputEvent({
        type: "mouseEvent",
        eventType: "mouseReleased",
        x: coords.x,
        y: coords.y,
        button: mouseButtonName(e.button),
        clickCount: 1,
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
      const now = Date.now();
      if (now - lastMouseMoveRef.current < 50) return;
      lastMouseMoveRef.current = now;
      const coords = mapMouseCoordinates(e, viewportWidth, viewportHeight);
      if (!coords) return;
      sendInputEvent({
        type: "mouseEvent",
        eventType: "mouseMoved",
        x: coords.x,
        y: coords.y,
        button: "none",
        clickCount: 0,
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

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!interactive || !userIsControlling) return;
      e.preventDefault();
      sendInputEvent({
        type: "keyEvent",
        eventType: "keyDown",
        key: e.key,
        code: e.code,
        text: e.key.length === 1 ? e.key : "",
        modifiers: getModifiers(e),
      });
    },
    [interactive, userIsControlling, sendInputEvent],
  );

  const handleKeyUp = useCallback(
    (e: React.KeyboardEvent) => {
      if (!interactive || !userIsControlling) return;
      e.preventDefault();
      sendInputEvent({
        type: "keyEvent",
        eventType: "keyUp",
        key: e.key,
        code: e.code,
        modifiers: getModifiers(e),
      });
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
    },
  };
}
