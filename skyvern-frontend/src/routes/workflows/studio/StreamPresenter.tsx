import { BrowserStream } from "@/components/BrowserStream";
import { useStreamTransport } from "@/hooks/useRuntimeConfig";
import { BrowserSessionStream } from "@/routes/browserSessions/BrowserSessionStream";
import type { StreamStateChangeHandler } from "@/routes/streaming/streamState";

type StreamPresenterProps = {
  browserSessionId: string;
  interactive?: boolean;
  showControlButtons?: boolean;
  // CDP only: turns the read-only URL bar into a navigation input while the user
  // holds control. VNC needs nothing here — it streams the browser's own chrome.
  enableUrlInput?: boolean;
  // Only the CDP transport carries the page URL; VNC is pixels-only and never
  // calls this.
  onUrlChange?: (url: string) => void;
  onStreamStateChange?: StreamStateChangeHandler;
  onActivity?: () => void;
};

/**
 * Transport-agnostic live browser stream: picks VNC vs CDP from runtime config.
 */
export function StreamPresenter({
  browserSessionId,
  interactive = false,
  showControlButtons = false,
  enableUrlInput = false,
  onUrlChange,
  onStreamStateChange,
  onActivity,
}: StreamPresenterProps) {
  const { streamTransport } = useStreamTransport(browserSessionId);
  if (!streamTransport) {
    // Mounting either transport now would open a connection this session may not serve.
    return null;
  }

  if (streamTransport === "cdp") {
    // CDP frames must be explicitly centered; VNC handles this in its own CSS.
    return (
      <div className="relative h-full w-full">
        <BrowserSessionStream
          browserSessionId={browserSessionId}
          interactive={interactive}
          showControlButtons={showControlButtons}
          enableUrlInput={enableUrlInput}
          onUrlChange={onUrlChange}
          onStreamStateChange={onStreamStateChange}
          onActivity={onActivity}
          centered
        />
      </div>
    );
  }

  return (
    <BrowserStream
      browserSessionId={browserSessionId}
      interactive={interactive}
      showControlButtons={showControlButtons}
      onStreamStateChange={onStreamStateChange}
      onActivity={onActivity}
    />
  );
}
