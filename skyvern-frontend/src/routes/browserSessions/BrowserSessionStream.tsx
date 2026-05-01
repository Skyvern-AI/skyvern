import { useEffect, useRef, useState } from "react";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { newWssBaseUrl, getCredentialParam } from "@/util/env";
import { useCdpInput } from "@/routes/streaming/useCdpInput";
import { InteractiveStreamView } from "@/routes/streaming/InteractiveStreamView";

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
  const credentialGetter = useCredentialGetter();

  const socketRef = useRef<WebSocket | null>(null);

  const inputWsUrl = interactive
    ? `${newWssBaseUrl}/stream/cdp_input/browser_session/${browserSessionId}`
    : null;

  const {
    userIsControlling,
    setUserIsControlling,
    inputReady,
    containerRef,
    handlers,
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
        containerRef={containerRef}
        showControlButtons={showControlButtons}
        handlers={handlers}
        currentUrl={currentUrl}
      />
    );
  }

  return (
    <div className="flex h-full w-full items-center justify-center text-sm text-slate-400">
      Starting stream...
    </div>
  );
}

export { BrowserSessionStream };
