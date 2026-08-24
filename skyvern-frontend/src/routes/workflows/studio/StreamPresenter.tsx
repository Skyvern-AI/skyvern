import { useLayoutEffect } from "react";

import { BrowserStream } from "@/components/BrowserStream";
import { RecordingPill } from "@/components/RecordingPill";
import { useStreamTransport } from "@/hooks/useRuntimeConfig";
import { BrowserSessionStream } from "@/routes/browserSessions/BrowserSessionStream";
import { useRecordingStore } from "@/store/useRecordingStore";

type StreamPresenterProps = {
  browserSessionId: string;
  interactive?: boolean;
  showControlButtons?: boolean;
  isRecording?: boolean;
  hideRecordingIndicator?: boolean;
  // CDP only: turns the read-only URL bar into a navigation input while the user
  // holds control. VNC needs nothing here — it streams the browser's own chrome.
  enableUrlInput?: boolean;
  // Only the CDP transport carries the page URL; VNC is pixels-only and never
  // calls this.
  onUrlChange?: (url: string) => void;
  onActivity?: () => void;
};

/**
 * Transport-agnostic live browser stream: picks VNC vs CDP from runtime config.
 * Recording stays on the session's own transport — a CDP-transport session has
 * no reachable VNC endpoint, so swapping to VNC for recording kills the view;
 * exfiltration only needs the messages channel, which both transports carry.
 */
export function StreamPresenter({
  browserSessionId,
  interactive = false,
  showControlButtons = false,
  isRecording = false,
  hideRecordingIndicator = false,
  enableUrlInput = false,
  onUrlChange,
  onActivity,
}: StreamPresenterProps) {
  const { streamTransport } = useStreamTransport(browserSessionId);
  const workflowPermanentId = useRecordingStore(
    (state) => state.workflowPermanentId,
  );
  const finishRequested = useRecordingStore((state) => state.finishRequested);
  const setRecordingTransport = useRecordingStore(
    (state) => state.setRecordingTransport,
  );

  // Recording telemetry labels each recording with its transport; the store
  // ignores this while a recording is live.
  useLayoutEffect(() => {
    if (streamTransport) {
      setRecordingTransport(streamTransport);
    }
  }, [streamTransport, setRecordingTransport]);

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
          // undefined keeps the recording message channel closed on the
          // non-recording live view; a defined value opens it.
          exfiltrate={isRecording ? !finishRequested : undefined}
          workflowPermanentId={workflowPermanentId}
          onUrlChange={onUrlChange}
          onActivity={onActivity}
          centered
        />
        {isRecording && !hideRecordingIndicator && (
          <div className="pointer-events-none absolute left-3 top-3 z-10">
            <RecordingPill />
          </div>
        )}
      </div>
    );
  }

  return (
    <BrowserStream
      browserSessionId={browserSessionId}
      interactive={interactive}
      showControlButtons={showControlButtons}
      exfiltrate={isRecording}
      hideRecordingIndicator={hideRecordingIndicator}
      // StrictMode remounts this component; the recording must survive those.
      // StudioBrowserStream owns the session-level reset instead.
      resetRecordingOnUnmount={false}
      onActivity={onActivity}
    />
  );
}
