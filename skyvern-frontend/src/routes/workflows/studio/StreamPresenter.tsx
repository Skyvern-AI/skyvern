import { BrowserStream } from "@/components/BrowserStream";
import { useBrowserStreamingMode } from "@/hooks/useRuntimeConfig";
import { BrowserSessionStream } from "@/routes/browserSessions/BrowserSessionStream";

type StreamPresenterProps = {
  browserSessionId: string;
  interactive?: boolean;
  showControlButtons?: boolean;
  isRecording?: boolean;
  // Only the CDP transport carries the page URL; VNC is pixels-only and never
  // calls this.
  onUrlChange?: (url: string) => void;
  onActivity?: () => void;
};

/**
 * Transport-agnostic live browser stream: picks VNC vs CDP from runtime config,
 * honoring the recording override (recording requires VNC).
 */
export function StreamPresenter({
  browserSessionId,
  interactive = false,
  showControlButtons = false,
  isRecording = false,
  onUrlChange,
  onActivity,
}: StreamPresenterProps) {
  const { browserStreamingMode } = useBrowserStreamingMode();
  const useCdp = browserStreamingMode === "cdp" && !isRecording;

  if (useCdp) {
    // CDP frames must be explicitly centered; VNC handles this in its own CSS.
    return (
      <BrowserSessionStream
        browserSessionId={browserSessionId}
        interactive={interactive}
        showControlButtons={showControlButtons}
        onUrlChange={onUrlChange}
        onActivity={onActivity}
        centered
      />
    );
  }

  return (
    <BrowserStream
      browserSessionId={browserSessionId}
      interactive={interactive}
      showControlButtons={showControlButtons}
      exfiltrate={isRecording}
      onActivity={onActivity}
    />
  );
}
