import { useCallback, useEffect, useRef, useState } from "react";
import { GlobeIcon } from "@radix-ui/react-icons";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { newWssBaseUrl, getRuntimeApiKey } from "@/util/env";
import { useClientIdStore } from "@/store/useClientIdStore";
import { Button } from "@/components/ui/button";
import { cn } from "@/util/utils";

type StreamMessage = {
  browser_session_id?: string;
  status: string;
  screenshot?: string;
  format?: string;
  viewport_width?: number;
  viewport_height?: number;
  url?: string;
};

interface Props {
  browserSessionId: string;
  interactive?: boolean;
  showControlButtons?: boolean;
}

function mouseButtonName(button: number): string {
  if (button === 2) return "right";
  if (button === 1) return "middle";
  return "left";
}

function getModifiers(
  e: React.MouseEvent | React.KeyboardEvent | React.WheelEvent,
): number {
  let m = 0;
  if (e.altKey) m |= 1;
  if (e.ctrlKey) m |= 2;
  if (e.metaKey) m |= 4;
  if (e.shiftKey) m |= 8;
  return m;
}

function mapCoordinates(
  e: React.MouseEvent<HTMLImageElement>,
  vpW: number,
  vpH: number,
): { x: number; y: number } | null {
  const rect = e.currentTarget.getBoundingClientRect();
  const containerAspect = rect.width / rect.height;
  const imageAspect = vpW / vpH;

  let renderedW: number, renderedH: number, offsetX: number, offsetY: number;
  if (containerAspect > imageAspect) {
    renderedH = rect.height;
    renderedW = rect.height * imageAspect;
    offsetX = (rect.width - renderedW) / 2;
    offsetY = 0;
  } else {
    renderedW = rect.width;
    renderedH = rect.width / imageAspect;
    offsetX = 0;
    offsetY = (rect.height - renderedH) / 2;
  }

  const localX = e.clientX - rect.left - offsetX;
  const localY = e.clientY - rect.top - offsetY;

  if (localX < 0 || localX > renderedW || localY < 0 || localY > renderedH) {
    return null;
  }

  return {
    x: Math.round(localX * (vpW / renderedW)),
    y: Math.round(localY * (vpH / renderedH)),
  };
}

function BrowserSessionStream({
  browserSessionId,
  interactive = false,
  showControlButtons = false,
}: Props) {
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
  const [streamFormat, setStreamFormat] = useState<string>("png");
  const [viewportWidth, setViewportWidth] = useState(1280);
  const [viewportHeight, setViewportHeight] = useState(720);
  const [userIsControlling, setUserIsControlling] = useState(false);
  const [inputReady, setInputReady] = useState(false);
  const [currentUrl, setCurrentUrl] = useState("");
  const credentialGetter = useCredentialGetter();
  const clientId = useClientIdStore((s) => s.clientId);

  const socketRef = useRef<WebSocket | null>(null);
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
    inputStoppedRef.current = false;
    inputReconnectAttemptsRef.current = 0;

    const RECONNECTABLE_CODES = new Set([1006, 1011, 4408, 4410, 4411]);

    async function getCredentialParam() {
      if (credentialGetter) {
        const token = await credentialGetter();
        return `token=Bearer ${token}`;
      }
      const apiKey = getRuntimeApiKey();
      return apiKey ? `apikey=${apiKey}` : "";
    }

    function connectInputWs(credentialParam: string) {
      if (inputStoppedRef.current) return;
      if (inputSocketRef.current) {
        inputSocketRef.current.close();
      }
      const ws = new WebSocket(
        `${newWssBaseUrl}/stream/cdp_input/browser_session/${browserSessionId}?client_id=${clientId}&${credentialParam}`,
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
        const credentialParam = await getCredentialParam();
        connectInputWs(credentialParam);
      } catch (e) {
        console.error("[cdp-input] Failed to get credentials for reconnect", e);
      }
    }

    async function run() {
      const credentialParam = await getCredentialParam();

      if (socketRef.current) {
        socketRef.current.close();
      }
      socketRef.current = new WebSocket(
        `${newWssBaseUrl}/stream/browser_sessions/${browserSessionId}?${credentialParam}`,
      );

      socketRef.current.addEventListener("message", (event) => {
        try {
          const message: StreamMessage = JSON.parse(event.data);
          if (message.screenshot) {
            setStreamImgSrc(message.screenshot);
          }
          if (message.format) {
            setStreamFormat(message.format);
          }
          if (message.viewport_width) {
            setViewportWidth(message.viewport_width);
          }
          if (message.viewport_height) {
            setViewportHeight(message.viewport_height);
          }
          if (message.url !== undefined) {
            setCurrentUrl(message.url);
          }
          if (
            message.status === "completed" ||
            message.status === "failed" ||
            message.status === "timeout"
          ) {
            socketRef.current?.close();
          }
        } catch (e) {
          console.error("Failed to parse message", e);
        }
      });

      socketRef.current.addEventListener("close", () => {
        socketRef.current = null;
      });

      if (interactive && browserSessionId) {
        connectInputWs(credentialParam);
      }
    }
    run();

    return () => {
      inputStoppedRef.current = true;
      if (inputReconnectTimerRef.current) {
        clearTimeout(inputReconnectTimerRef.current);
        inputReconnectTimerRef.current = null;
      }
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
      if (inputSocketRef.current) {
        inputSocketRef.current.close();
        inputSocketRef.current = null;
      }
    };
  }, [credentialGetter, browserSessionId, interactive, clientId]);

  // Keep ref in sync for use in WS callbacks
  useEffect(() => {
    userIsControllingRef.current = userIsControlling;
  }, [userIsControlling]);

  // Send take-control / cede-control when userIsControlling changes
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

  // Focus management
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
      const containerAspect = rect.width / rect.height;
      const imageAspect = viewportWidth / viewportHeight;

      let renderedW: number,
        renderedH: number,
        offsetX: number,
        offsetY: number;
      if (containerAspect > imageAspect) {
        renderedH = rect.height;
        renderedW = rect.height * imageAspect;
        offsetX = (rect.width - renderedW) / 2;
        offsetY = 0;
      } else {
        renderedW = rect.width;
        renderedH = rect.width / imageAspect;
        offsetX = 0;
        offsetY = (rect.height - renderedH) / 2;
      }

      const localX = e.clientX - rect.left - offsetX;
      const localY = e.clientY - rect.top - offsetY;

      if (
        localX < 0 ||
        localX > renderedW ||
        localY < 0 ||
        localY > renderedH
      ) {
        return;
      }

      const x = Math.round(localX * (viewportWidth / renderedW));
      const y = Math.round(localY * (viewportHeight / renderedH));

      let modifiers = 0;
      if (e.altKey) modifiers |= 1;
      if (e.ctrlKey) modifiers |= 2;
      if (e.metaKey) modifiers |= 4;
      if (e.shiftKey) modifiers |= 8;

      ws.send(
        JSON.stringify({
          type: "wheelEvent",
          x,
          y,
          deltaX: Math.round(e.deltaX),
          deltaY: Math.round(e.deltaY),
          modifiers,
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
      const coords = mapCoordinates(e, viewportWidth, viewportHeight);
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
      const coords = mapCoordinates(e, viewportWidth, viewportHeight);
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
      const coords = mapCoordinates(e, viewportWidth, viewportHeight);
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

  if (streamImgSrc.length > 0) {
    if (interactive) {
      return (
        <div
          ref={containerRef}
          className="relative h-full w-full outline-none"
          tabIndex={0}
          onKeyDown={handleKeyDown}
          onKeyUp={handleKeyUp}
        >
          {currentUrl && (
            <div className="flex h-8 w-full items-center gap-2 rounded-t-md bg-slate-800 px-3 text-xs text-slate-300">
              <GlobeIcon className="h-3 w-3 flex-shrink-0 text-slate-400" />
              <span className="truncate">{currentUrl}</span>
            </div>
          )}
          {showControlButtons && !userIsControlling && inputReady && (
            <div className="absolute inset-0 z-10 flex items-center justify-center">
              <Button onClick={() => setUserIsControlling(true)}>
                take control
              </Button>
            </div>
          )}
          {showControlButtons && userIsControlling && (
            <Button
              className="absolute bottom-2 left-1/2 z-10 -translate-x-1/2"
              onClick={() => setUserIsControlling(false)}
            >
              stop controlling
            </Button>
          )}
          <img
            src={`data:image/${streamFormat};base64,${streamImgSrc}`}
            className={cn(
              "w-full rounded-md object-contain",
              currentUrl ? "h-[calc(100%-2rem)]" : "h-full",
              { "cursor-default": userIsControlling },
            )}
            onMouseDown={handleMouseDown}
            onMouseUp={handleMouseUp}
            onMouseMove={handleMouseMove}
            onContextMenu={(e) => e.preventDefault()}
            draggable={false}
          />
        </div>
      );
    }

    return (
      <div className="h-full w-full">
        {currentUrl && (
          <div className="flex h-8 w-full items-center gap-2 rounded-t-md bg-slate-800 px-3 text-xs text-slate-300">
            <GlobeIcon className="h-3 w-3 flex-shrink-0 text-slate-400" />
            <span className="truncate">{currentUrl}</span>
          </div>
        )}
        <ZoomableImage
          src={`data:image/${streamFormat};base64,${streamImgSrc}`}
          className={
            currentUrl ? "h-[calc(100%-2rem)] rounded-b-md" : "rounded-md"
          }
        />
      </div>
    );
  }

  return (
    <div className="flex h-full w-full items-center justify-center text-sm text-slate-400">
      Starting stream...
    </div>
  );
}

export { BrowserSessionStream };
