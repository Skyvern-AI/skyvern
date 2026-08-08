import { useEffect, useState } from "react";

import { BrowserStream } from "@/components/BrowserStream";
import { useStreamTransport } from "@/hooks/useRuntimeConfig";

import { WorkflowRunStream } from "../../workflowRun/WorkflowRunStream";

type RunLiveStreamProps = {
  workflowRunId: string;
  browserSessionId: string | null;
  interactive: boolean;
  // Live page URL from the CDP frames; the VNC path doesn't surface one yet.
  onUrlChange?: (url: string) => void;
};

/**
 * Live browser for a workflow run, mirroring WorkflowRunOverview: VNC keyed by the
 * browser session, with a CDP fallback when VNC is wrong or closes early.
 */
export function RunLiveStream({
  workflowRunId,
  browserSessionId,
  interactive,
  onUrlChange,
}: RunLiveStreamProps) {
  const { streamTransport } = useStreamTransport(browserSessionId);
  const [vncFailed, setVncFailed] = useState(false);

  useEffect(() => {
    setVncFailed(false);
  }, [browserSessionId]);

  const useVnc =
    Boolean(browserSessionId) && streamTransport !== "cdp" && !vncFailed;

  if (browserSessionId && !streamTransport) {
    // Defaulting to VNC here opens a socket an externally hosted session cannot serve, and the
    // failure is what swaps the transport — a visible drop rather than a first paint.
    return null;
  }

  if (useVnc && browserSessionId) {
    return (
      <BrowserStream
        key={browserSessionId}
        browserSessionId={browserSessionId}
        interactive={interactive}
        showControlButtons={interactive}
        // A recording can be live while this per-run stream mounts and unmounts;
        // StudioBrowserStream owns the session-level reset.
        resetRecordingOnUnmount={false}
        onClose={() => setVncFailed(true)}
      />
    );
  }

  return (
    <WorkflowRunStream
      workflowRunId={workflowRunId}
      alwaysShowStream
      interactive={interactive}
      showControlButtons={interactive}
      onUrlChange={onUrlChange}
      centered
    />
  );
}
