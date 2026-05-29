import { useEffect, useRef, useState } from "react";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { newWssBaseUrl, getCredentialParam } from "@/util/env";
import { useCdpInput } from "@/routes/streaming/useCdpInput";
import { InteractiveStreamView } from "@/routes/streaming/InteractiveStreamView";
import {
  StreamStatusPanel,
  type StreamDiagnostic,
} from "@/routes/streaming/StreamDiagnostics";

type StreamMessage = {
  browser_session_id?: string;
  status: string;
  screenshot?: string;
  format?: string;
  viewport_width?: number;
  viewport_height?: number;
  url?: string;
};

const STARTING_DIAGNOSTIC: StreamDiagnostic = {
  title: "Starting local browser stream",
  detail:
    "Opening the stream WebSocket and waiting for the first browser frame.",
};

function diagnosticForStatus(status: string): StreamDiagnostic {
  switch (status) {
    case "not_found":
      return {
        title: "Browser session not found",
        detail:
          "The backend could not find this browser session for the current organization.",
        hint: "Refresh the page or create a new browser session.",
      };
    case "timeout":
      return {
        title: "Timed out waiting for browser state",
        detail:
          "The stream connected, but the backend did not find an active page to screencast.",
        hint: "Check backend logs for browser launch errors and verify BROWSER_STREAMING_MODE=cdp.",
      };
    case "completed":
    case "failed":
      return {
        title: "Browser session is no longer live",
        detail: `The browser session status is ${status}.`,
      };
    default:
      return {
        title: "Waiting for browser frames",
        detail: `The stream is connected and the session status is ${status}.`,
      };
  }
}

function diagnosticForClose(event: CloseEvent): StreamDiagnostic {
  if (event.code === 4001 || event.reason === "use-vnc-streaming") {
    return {
      title: "Backend is using VNC streaming",
      detail:
        "The UI tried local browser streaming, but the backend closed the stream with use-vnc-streaming.",
      hint: "Check BROWSER_STREAMING_MODE on the backend and the runtime config response.",
    };
  }
  if (event.code === 1006) {
    return {
      title: "Stream connection dropped",
      detail: "The browser stream WebSocket closed before sending a frame.",
      hint: "Check that the API server is running and reachable from the UI.",
    };
  }
  return {
    title: "Stream connection closed",
    detail: `WebSocket closed with code ${event.code}${event.reason ? ` (${event.reason})` : ""}.`,
  };
}

interface Props {
  browserSessionId: string;
  interactive?: boolean;
  showControlButtons?: boolean;
  onReadyChange?: (isReady: boolean, browserSessionId: string | null) => void;
}

function BrowserSessionStream({
  browserSessionId,
  interactive = false,
  showControlButtons = false,
  onReadyChange,
}: Props) {
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
  const [streamFormat, setStreamFormat] = useState<string>("png");
  const [viewportWidth, setViewportWidth] = useState(1280);
  const [viewportHeight, setViewportHeight] = useState(720);
  const [currentUrl, setCurrentUrl] = useState("");
  const [diagnostic, setDiagnostic] =
    useState<StreamDiagnostic>(STARTING_DIAGNOSTIC);
  const credentialGetter = useCredentialGetter();

  const socketRef = useRef<WebSocket | null>(null);
  const hasFrameRef = useRef(false);

  const inputWsUrl = interactive
    ? `${newWssBaseUrl}/stream/cdp_input/browser_session/${browserSessionId}`
    : null;

  const {
    userIsControlling,
    setUserIsControlling,
    inputReady,
    browserCommandError,
    containerRef,
    handlers,
    browserControls,
  } = useCdpInput({
    inputWsUrl,
    interactive,
    viewportWidth,
    viewportHeight,
  });

  useEffect(() => {
    let cancelled = false;
    setStreamImgSrc("");
    setStreamFormat("png");
    setViewportWidth(1280);
    setViewportHeight(720);
    setCurrentUrl("");
    setDiagnostic(STARTING_DIAGNOSTIC);
    hasFrameRef.current = false;

    async function run() {
      const credentialParam = await getCredentialParam(credentialGetter);
      if (cancelled) {
        return;
      }

      if (socketRef.current) {
        socketRef.current.close();
      }
      socketRef.current = new WebSocket(
        `${newWssBaseUrl}/stream/browser_sessions/${browserSessionId}?${credentialParam}`,
      );

      socketRef.current.addEventListener("open", () => {
        setDiagnostic({
          title: "Connected to stream",
          detail: "Waiting for the backend to attach to the browser page.",
        });
      });

      socketRef.current.addEventListener("message", (event) => {
        try {
          const message: StreamMessage = JSON.parse(event.data);
          if (message.screenshot) {
            hasFrameRef.current = true;
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
          if (!message.screenshot && message.status) {
            setDiagnostic(diagnosticForStatus(message.status));
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
          setDiagnostic({
            title: "Unexpected stream message",
            detail: "The browser stream sent a message the UI could not parse.",
          });
        }
      });

      socketRef.current.addEventListener("error", () => {
        setDiagnostic({
          title: "Stream WebSocket error",
          detail:
            "The browser stream connection hit a network or server error.",
        });
      });

      socketRef.current.addEventListener("close", (event) => {
        if (!cancelled && !hasFrameRef.current) {
          setDiagnostic(diagnosticForClose(event));
        }
        socketRef.current = null;
      });
    }
    run();

    return () => {
      cancelled = true;
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
    };
  }, [credentialGetter, browserSessionId]);

  const isReady = streamImgSrc.length > 0;

  useEffect(() => {
    // browserSessionId intentionally not a dep: re-firing on prop change
    // before isReady resets would spuriously report (true, newSessionId).
    onReadyChange?.(isReady, isReady ? browserSessionId : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isReady, onReadyChange]);

  useEffect(() => {
    return () => {
      onReadyChange?.(false, null);
    };
  }, [onReadyChange]);

  if (isReady) {
    return (
      <InteractiveStreamView
        streamImgSrc={streamImgSrc}
        streamFormat={streamFormat}
        interactive={interactive}
        userIsControlling={userIsControlling}
        setUserIsControlling={setUserIsControlling}
        inputReady={inputReady}
        browserCommandError={interactive ? browserCommandError : null}
        containerRef={containerRef}
        showControlButtons={showControlButtons}
        handlers={handlers}
        browserControls={interactive ? browserControls : undefined}
        currentUrl={currentUrl}
      />
    );
  }

  return <StreamStatusPanel diagnostic={diagnostic} />;
}

export { BrowserSessionStream };
